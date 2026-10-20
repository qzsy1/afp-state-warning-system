[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Inventory", "Stage", "Switchover", "FinalizeSwitchover", "StartupPreview", "StartupApply", "Evidence", "CleanupPreview", "Cleanup", "Report", "Rollback")]
    [string]$Mode,
    [string]$WorkspaceRoot = "",
    [string]$ReferenceRelease = "",
    [string]$StagingPath = "",
    [string]$ApprovedInventoryDigest = "",
    [string]$PublicUrl = "",
    [string]$LanIPv4 = "",
    [string]$FunnelOrigin = "http://127.0.0.1:8770",
    [string]$ExpectedCandidateId = "",
    [string]$GitRef = "HEAD",
    [string]$EvidenceSource = "",
    [string[]]$CleanupTargets = @(),
    [string]$PreviewPath = "",
    [switch]$LegacyBootstrap,
    [switch]$AllowPendingHelper
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "SingleRuntimePaths.psm1") -Force
Import-Module (Join-Path $PSScriptRoot "SingleRuntimeRelease.psm1") -Force
Import-Module (Join-Path $PSScriptRoot "SingleRuntimeDeployment.psm1") -Force
Import-Module (Join-Path $PSScriptRoot "SingleRuntimeEvidence.psm1") -Force
$configPath = Join-Path $PSScriptRoot "single-runtime-paths.json"
$paths = if ($WorkspaceRoot) {
    Get-AfpDeploymentPaths -ConfigPath $configPath -WorkspaceRootOverride $WorkspaceRoot
} else {
    Get-AfpDeploymentPaths -ConfigPath $configPath
}
$evidenceDir = Join-Path $paths.EvidenceRoot "SINGLE-RUNTIME-MIGRATION"
$legacyTargets = @(
    "D:\AFP-Main-Candidate-16e887f",
    "D:\AFP-Main-Candidate-2585ca",
    "D:\AFP-Main-Evidence\CROSS-20261013-01"
)

function Write-State([string]$Name, [psobject]$Value) {
    New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null
    $path = Join-Path $evidenceDir $Name
    $Value | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $path -Encoding UTF8
    return $path
}

if ($Mode -eq "Inventory") {
    $runtimeInventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    $inventory = Get-AfpMigrationInventory -Paths $paths -RuntimeInventory $runtimeInventory -ExternalTargets $legacyTargets
    $path = Write-State "inventory.json" $inventory
    [pscustomobject]@{ status = "inventoried"; inventory = $inventory; evidence = $path } | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -in @("Stage", "Rollback")) {
    $ReferenceRelease = Resolve-AfpReferenceRelease -Paths $paths -RequestedPath $ReferenceRelease
    $readiness = Test-AfpBuildReadiness -Paths $paths -RepoRoot $paths.SourceRoot -GitRef $GitRef -RequiredInputs @(
        (Join-Path $paths.SourceRoot "pre_sensor\release-contract.json"),
        (Join-Path $paths.SourceRoot "modular_runtime\assemble_modular_delivery.ps1")
    ) -ReferenceRelease $ReferenceRelease
    if ($readiness.status -ne "passed") {
        $readiness | ConvertTo-Json -Depth 10
        exit 2
    }
    $staged = if ($Mode -eq "Rollback") {
        New-AfpRollbackStage -Paths $paths -GitRef $GitRef -ReferenceRelease $ReferenceRelease
    } else {
        New-AfpStagedRelease -Paths $paths -ReferenceRelease $ReferenceRelease
    }
    $gate = Test-AfpReleaseGate -CandidatePath $staged.staging_path -ExpectedCandidateId $staged.candidate_id
    if ($gate.status -ne "passed") {
        Remove-AfpDeploymentStaging -StagingPath $staged.staging_path -Paths $paths
        $gate | ConvertTo-Json -Depth 10
        exit 3
    }
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    $approval = New-AfpApprovalSummary -Inventory $inventory -Operation Switchover
    $state = [pscustomobject]@{ status = "approval_required"; mode = $Mode.ToLowerInvariant(); git_ref = $GitRef; staged = $staged; gate = $gate; approval = $approval }
    $path = Write-State "stage.json" $state
    [pscustomobject]@{ state = $state; evidence = $path } | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -eq "Switchover") {
    if (-not $ApprovedInventoryDigest -or -not $StagingPath) { throw "Switchover requires ApprovedInventoryDigest and StagingPath." }
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    if ($inventory.digest -ne $ApprovedInventoryDigest) { throw "Runtime inventory changed after approval." }
    $exe = Join-Path $paths.RuntimeRoot "AFP_Integrated_System_Modular.exe"
    $managedProcesses = @($inventory.processes | Where-Object {
        $_.executable_path -and
        [string]::Equals([IO.Path]::GetFullPath([string]$_.executable_path), [IO.Path]::GetFullPath($exe), [StringComparison]::OrdinalIgnoreCase)
    })
    $listener = @($inventory.listeners | Where-Object { $_.port -eq 8770 } | Select-Object -First 1)
    if ($listener.Count) {
        $listenerProcess = @($managedProcesses | Where-Object { $_.pid -eq $listener[0].pid } | Select-Object -First 1)
        if (-not $listenerProcess.Count) { throw "The approved 8770 listener is not a fixed-runtime AFP process." }
    }
    $script:oldProcesses = @()
    $script:legacyBootstrapStopped = $false
    $script:watchdogWasRunning = $false
    $script:stoppedHelperProcesses = @()
    $script:activePromotion = $null
    $script:verifiedServices = $null
    $failureEvidence = Join-Path $evidenceDir "switchover-failure.json"
    $rollbackEvidence = Join-Path $evidenceDir "switchover-rollback.json"
    $legacyBootstrapRunner = if ($LegacyBootstrap) {
        {
            $localStatus = try {
                Invoke-RestMethod -Uri "http://127.0.0.1:8771/api/acquisition/status" -Method Get -TimeoutSec 5
            } catch {
                return [pscustomobject]@{ allowed = $false; error = "legacy_local_status_unavailable" }
            }
            if ([bool]$localStatus.running) {
                return [pscustomobject]@{ allowed = $false; error = "legacy_active_local_capture" }
            }

            $currentManifest = Get-Content -LiteralPath (Join-Path $paths.RuntimeRoot "RELEASE-MANIFEST.json") `
                -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
            $currentCandidateId = [string]$currentManifest.candidate.candidate_id
            Import-Module (Join-Path $paths.SourceRoot "visualization_app\PublicIngress.psm1") -Force -ErrorAction Stop
            $legacyPublicEdgeProbe = {
                param($Name, $Target, $ExpectedHash)
                try {
                    $targetUri = [Uri]$Target
                    foreach ($address in @(Resolve-AfpPublicEdgeAddresses -HostName $targetUri.DnsSafeHost)) {
                        $probe = Invoke-AfpForcedPublicProbe -Uri $targetUri -Address $address `
                            -ExpectedSha256 $ExpectedHash
                        if ([bool]$probe.ok) {
                            return [pscustomobject]@{ passed = $true; detail = "ok" }
                        }
                    }
                } catch {}
                return [pscustomobject]@{ passed = $false; detail = "forced public edge probe failed" }
            }
            $currentServices = Test-AfpRuntimeServices -RuntimeRoot $paths.RuntimeRoot `
                -PublicUrl $PublicUrl -LanIPv4 $LanIPv4 -ExpectedCandidateId $currentCandidateId `
                -FunnelOrigin $FunnelOrigin -PublicEdgeProbeRunner $legacyPublicEdgeProbe
            if ([string]$currentServices.status -ne "passed") {
                return [pscustomobject]@{ allowed = $false; error = "legacy_current_service_gate_failed" }
            }

            $statePath = Join-Path $paths.RuntimeRoot "runtime\public-ingress-state.json"
            if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
                $layers = @{}
                foreach ($check in @($currentServices.checks)) {
                    $layers[[string]$check.name] = @{ ok = [bool]$check.passed; code = [string]$check.detail }
                }
                $layers.ingress = @{ ok = $true; code = "legacy_ingress_initialized" }
                $layers.helper = @{ ok = $false; code = "helper_reconciliation_required" }
                $hostName = ([Uri]$PublicUrl).DnsSafeHost.ToLowerInvariant()
                $provider = if ($hostName.EndsWith(".ts.net")) { "tailscale" } `
                    elseif ($hostName.EndsWith(".trycloudflare.com")) { "cloudflare" } `
                    else { "external" }
                Publish-AfpPublicIngressState -Path $statePath -PublicUrl $PublicUrl `
                    -Provider $provider -RecoveryOwner $provider -CandidateId $currentCandidateId `
                    -Layers $layers | Out-Null
            }

            $watchdog = Get-ScheduledTask -TaskName "AFP Public Tunnel Watchdog" -ErrorAction SilentlyContinue
            $script:watchdogWasRunning = $null -ne $watchdog -and [string]$watchdog.State -eq "Running"
            if ($script:watchdogWasRunning) {
                Stop-ScheduledTask -TaskName "AFP Public Tunnel Watchdog" -ErrorAction Stop
            }
            $runtimePrefix = [IO.Path]::GetFullPath($paths.RuntimeRoot).TrimEnd('\') + '\'
            $script:stoppedHelperProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
                $_.Name -eq "AFP_Local_Capture_Helper.exe" -and $_.ExecutablePath -and
                [IO.Path]::GetFullPath([string]$_.ExecutablePath).StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)
            } | Select-Object ProcessId, ExecutablePath, CommandLine)
            foreach ($helperProcess in $script:stoppedHelperProcesses) {
                Stop-Process -Id ([int]$helperProcess.ProcessId) -Force -ErrorAction Stop
                Wait-Process -Id ([int]$helperProcess.ProcessId) -Timeout 15 -ErrorAction SilentlyContinue
            }
            $script:oldProcesses = @(Stop-AfpManagedRuntimeProcesses -Inventory $inventory -RuntimeExecutable $exe)
            $script:legacyBootstrapStopped = $true

            $python = Join-Path $paths.SourceRoot ".venv\Scripts\python.exe"
            $reservationText = @(& $python (Join-Path $paths.SourceRoot "pre_sensor\legacy-cutover-reservation.py") `
                --runtime-root $paths.RuntimeRoot --candidate-id $currentCandidateId --ttl-seconds 600 2>$null)
            $reservationExit = $LASTEXITCODE
            $reservation = if ($reservationText.Count) {
                ($reservationText -join "`n") | ConvertFrom-Json -ErrorAction Stop
            } else {
                $null
            }
            if ($reservationExit -ne 0 -or -not $reservation -or -not [bool]$reservation.allowed) {
                Start-AfpManagedRuntimeProcesses -Processes $script:oldProcesses -RuntimeExecutable $exe `
                    -WorkingDirectory $paths.RuntimeRoot | Out-Null
                if ($script:watchdogWasRunning) {
                    Start-ScheduledTask -TaskName "AFP Public Tunnel Watchdog" -ErrorAction SilentlyContinue
                }
                $script:legacyBootstrapStopped = $false
                return [pscustomobject]@{ allowed = $false; error = "legacy_persisted_readiness_blocked" }
            }
            return $reservation
        }
    } else {
        $null
    }
    $pendingHelperRunner = if ($AllowPendingHelper) {
        {
            param($Ingress)
            try {
                Invoke-RestMethod -Uri "http://127.0.0.1:8771/api/ingress/helper-readiness" `
                    -Method Get -TimeoutSec 5
            } catch {
                [pscustomobject]@{ ready = $false; error = "helper_readiness_unavailable" }
            }
        }
    } else {
        $null
    }
    $transactionParameters = @{
        StagingPath = $StagingPath
        Paths = $paths
        ApprovedInventoryDigest = $ApprovedInventoryDigest
        CurrentInventory = $inventory
        FailureEvidencePath = $failureEvidence
        RollbackEvidencePath = $rollbackEvidence
        AllowPendingHelper = $AllowPendingHelper
    }
    if ($legacyBootstrapRunner) { $transactionParameters.LegacyBootstrapRunner = $legacyBootstrapRunner }
    if ($pendingHelperRunner) { $transactionParameters.HelperReadinessRunner = $pendingHelperRunner }
    $transaction = Invoke-AfpVerifiedRuntimePromotion @transactionParameters `
        -StopRunner {
            if (-not $script:legacyBootstrapStopped) {
                $script:oldProcesses = @(Stop-AfpManagedRuntimeProcesses -Inventory $inventory -RuntimeExecutable $exe)
            }
        } `
        -PromotionRunner {
            $script:activePromotion = Invoke-AfpRuntimePromotion -StagingPath $StagingPath -Paths $paths `
                -ApprovedInventoryDigest $ApprovedInventoryDigest -CurrentInventory $inventory
            return $script:activePromotion
        } `
        -StartRunner {
            $restartPlan = if ($script:oldProcesses.Count) {
                $script:oldProcesses
            } else {
                @([pscustomobject]@{ pid = 0; launch_arguments = "--server-only" })
            }
            Start-AfpManagedRuntimeProcesses -Processes $restartPlan -RuntimeExecutable $exe `
                -WorkingDirectory $paths.RuntimeRoot | Out-Null
        } `
        -ServiceProbeRunner {
            $script:verifiedServices = Wait-AfpRuntimeServices -RuntimeRoot $paths.RuntimeRoot `
                -PublicUrl $PublicUrl -LanIPv4 $LanIPv4 -ExpectedCandidateId $ExpectedCandidateId `
                -FunnelOrigin $FunnelOrigin -TimeoutSeconds 90 -PollIntervalMilliseconds 1000
            return $script:verifiedServices
        } `
        -IngressPublisher {
            $modulePath = Join-Path $paths.RuntimeRoot "PublicIngress.psm1"
            Import-Module $modulePath -Force -ErrorAction Stop
            $statePath = Join-Path $paths.RuntimeRoot "runtime\public-ingress-state.json"
            $manifest = Get-Content -LiteralPath (Join-Path $paths.RuntimeRoot "RELEASE-MANIFEST.json") `
                -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
            $candidateId = [string]$manifest.candidate.candidate_id
            if ($ExpectedCandidateId -and $candidateId -cne $ExpectedCandidateId) {
                throw "runtime candidate identity mismatch"
            }
            $current = $null
            try { $current = Read-AfpPublicIngressState -Path $statePath }
            catch { $current = $null }
            $hostName = ([Uri]$PublicUrl).DnsSafeHost.ToLowerInvariant()
            $provider = if ($current) { [string]$current.provider } `
                elseif ($hostName.EndsWith(".ts.net")) { "tailscale" } `
                elseif ($hostName.EndsWith(".trycloudflare.com")) { "cloudflare" } `
                else { "external" }
            $owner = if ($current) { [string]$current.recovery_owner } else { $provider }
            $layers = @{}
            foreach ($check in @($script:verifiedServices.checks)) {
                $layers[[string]$check.name] = @{ ok = [bool]$check.passed; code = [string]$check.detail }
            }
            $layers.ingress = @{ ok = $false; code = "ingress_pending_helper" }
            $layers.helper = @{ ok = $false; code = "helper_not_checked" }
            $parameters = @{
                Path = $statePath
                PublicUrl = $PublicUrl
                Provider = $provider
                RecoveryOwner = $owner
                CandidateId = $candidateId
                Layers = $layers
            }
            if ($current) {
                $parameters.ExpectedGeneration = [long]$current.generation
                if ([string]$current.candidate_id -cne $candidateId) {
                    $parameters.ExpectedPreviousCandidateId = [string]$current.candidate_id
                    $parameters.MinimumGeneration = [long]$current.generation + 1L
                }
            }
            $published = Publish-AfpPublicIngressState @parameters
            Read-AfpPublicIngressState -Path $statePath -ExpectedCandidateId $candidateId | Out-Null
            return $published
        } `
        -IngressCommitter {
            param($Ingress, $Helper)
            $modulePath = Join-Path $paths.RuntimeRoot "PublicIngress.psm1"
            Import-Module $modulePath -Force -ErrorAction Stop
            $statePath = Join-Path $paths.RuntimeRoot "runtime\public-ingress-state.json"
            $pending = Read-AfpPublicIngressState -Path $statePath
            $layers = @{}
            foreach ($property in $pending.layers.PSObject.Properties) {
                $layers[$property.Name] = @{
                    ok = [bool]$property.Value.ok
                    code = [string]$property.Value.code
                }
            }
            $layers.ingress = @{ ok = $true; code = "ingress_committed" }
            $layers.helper = @{ ok = $true; code = "helper_ready" }
            Publish-AfpPublicIngressState -Path $statePath `
                -PublicUrl ([string]$pending.public_url) `
                -Provider ([string]$pending.provider) `
                -RecoveryOwner ([string]$pending.recovery_owner) `
                -CandidateId ([string]$pending.candidate_id) `
                -Layers $layers -ExpectedGeneration ([long]$Ingress.generation)
        } `
        -CompletionRunner {
            Complete-AfpRuntimePromotion -Promotion $script:activePromotion -Paths $paths
        } `
        -RestoreRunner {
            param($Promotion, $IngressSnapshot, $FailedIngressGeneration, $FailedIngressState)
            if ($null -eq $IngressSnapshot) {
                if ($FailedIngressState) {
                    Import-Module (Join-Path $paths.RuntimeRoot "PublicIngress.psm1") -Force -ErrorAction Stop
                    $failedLayers = @{
                        origin = @{ ok = $false; code = "rollback_no_previous_ingress" }
                        control_plane = @{ ok = $false; code = "rollback_no_previous_ingress" }
                        dns = @{ ok = $false; code = "rollback_no_previous_ingress" }
                        public_edge = @{ ok = $false; code = "rollback_no_previous_ingress" }
                        ingress = @{ ok = $false; code = "rollback_no_previous_ingress" }
                        helper = @{ ok = $false; code = "rollback_no_previous_ingress" }
                    }
                    Publish-AfpPublicIngressState -Path (Join-Path $paths.RuntimeRoot "runtime\public-ingress-state.json") `
                        -PublicUrl ([string]$FailedIngressState.public_url) `
                        -Provider ([string]$FailedIngressState.provider) `
                        -RecoveryOwner ([string]$FailedIngressState.recovery_owner) `
                        -CandidateId ([string]$FailedIngressState.candidate_id) `
                        -ExpectedGeneration ([long]$FailedIngressState.generation) `
                        -MinimumGeneration ([Math]::Max(1L, [long]$FailedIngressGeneration + 1L)) `
                        -Layers $failedLayers | Out-Null
                }
                return [pscustomobject]@{ status = "failed"; error = "rollback_no_previous_ingress" }
            }
            $failedInventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
            Stop-AfpManagedRuntimeProcesses -Inventory $failedInventory -RuntimeExecutable $exe | Out-Null
            if ($Promotion) { Restore-AfpRuntimePromotion -Promotion $Promotion -Paths $paths }
            $statePath = Join-Path $paths.RuntimeRoot "runtime\public-ingress-state.json"
            $snapshotText = [Text.Encoding]::UTF8.GetString([byte[]]$IngressSnapshot).TrimStart([char]0xFEFF)
            $snapshot = $snapshotText | ConvertFrom-Json -ErrorAction Stop
            Import-Module (Join-Path $paths.RuntimeRoot "PublicIngress.psm1") -Force -ErrorAction Stop
            $restored = Read-AfpPublicIngressState -Path $statePath
            $snapshotLayers = @{}
            foreach ($property in $snapshot.layers.PSObject.Properties) {
                $snapshotLayers[$property.Name] = @{
                    ok = [bool]$property.Value.ok
                    code = [string]$property.Value.code
                }
            }
            $snapshotLayers["helper"] = @{
                ok = $false
                code = "rollback_helper_reconciliation_required"
            }
            Publish-AfpPublicIngressState -Path $statePath `
                -PublicUrl ([string]$snapshot.public_url) `
                -Provider ([string]$snapshot.provider) `
                -RecoveryOwner ([string]$snapshot.recovery_owner) `
                -CandidateId ([string]$snapshot.candidate_id) `
                -ExpectedGeneration ([long]$restored.generation) `
                -MinimumGeneration ([Math]::Max([long]$restored.generation + 1L, [long]$FailedIngressGeneration + 1L)) `
                -Layers $snapshotLayers | Out-Null
            $rollbackPlan = if ($script:oldProcesses.Count) {
                $script:oldProcesses
            } else {
                @([pscustomobject]@{ pid = 0; launch_arguments = "--server-only" })
            }
            Start-AfpManagedRuntimeProcesses -Processes $rollbackPlan -RuntimeExecutable $exe `
                -WorkingDirectory $paths.RuntimeRoot | Out-Null
            $rollbackServices = Wait-AfpRuntimeServices -RuntimeRoot $paths.RuntimeRoot `
                -PublicUrl ([string]$snapshot.public_url) -LanIPv4 $LanIPv4 `
                -ExpectedCandidateId ([string]$snapshot.candidate_id) `
                -FunnelOrigin $FunnelOrigin -TimeoutSeconds 90 -PollIntervalMilliseconds 1000
            if ([string]$rollbackServices.status -ne "passed") {
                throw "rollback_service_gate_failed"
            }
            return $rollbackServices
        }
    if ($script:watchdogWasRunning) {
        Start-ScheduledTask -TaskName "AFP Public Tunnel Watchdog" -ErrorAction SilentlyContinue
    }
    if ($transaction.status -eq "awaiting_helper") {
        $pendingPath = Write-State "switchover-pending.json" $transaction
        [pscustomobject]@{ transaction = $transaction; evidence = $pendingPath } | ConvertTo-Json -Depth 12
        exit 0
    }
    $transaction | ConvertTo-Json -Depth 12
    if ($transaction.status -eq "completed") { exit 0 }
    if ($transaction.status -eq "blocked") { exit 5 }
    exit 4
}

if ($Mode -eq "FinalizeSwitchover") {
    if (-not $ExpectedCandidateId -or -not $PublicUrl) {
        throw "FinalizeSwitchover requires ExpectedCandidateId and PublicUrl."
    }
    if (-not $PreviewPath) { $PreviewPath = Join-Path $evidenceDir "switchover-pending.json" }
    if (-not (Test-Path -LiteralPath $PreviewPath -PathType Leaf)) {
        throw "Pending switchover evidence is missing."
    }
    $pendingEnvelope = Get-Content -LiteralPath $PreviewPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $pending = if ($pendingEnvelope.PSObject.Properties["transaction"]) { $pendingEnvelope.transaction } else { $pendingEnvelope }
    if ([string]$pending.status -ne "awaiting_helper") { throw "Switchover is not awaiting Helper completion." }
    $manifest = Get-Content -LiteralPath (Join-Path $paths.RuntimeRoot "RELEASE-MANIFEST.json") `
        -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    if ([string]$manifest.candidate.candidate_id -cne $ExpectedCandidateId) {
        throw "Runtime candidate identity mismatch."
    }
    $reservationPath = Join-Path $paths.RuntimeRoot "runtime\public-ingress-cutover.json"
    if (Test-Path -LiteralPath $reservationPath -PathType Leaf) {
        [pscustomobject]@{ status = "blocked"; error = "cutover_reservation_active" } | ConvertTo-Json
        exit 5
    }
    $services = Wait-AfpRuntimeServices -RuntimeRoot $paths.RuntimeRoot -PublicUrl $PublicUrl `
        -LanIPv4 $LanIPv4 -ExpectedCandidateId $ExpectedCandidateId -FunnelOrigin $FunnelOrigin `
        -TimeoutSeconds 30 -PollIntervalMilliseconds 1000
    if ([string]$services.status -ne "passed") { throw "runtime_service_gate_failed" }
    $helper = Invoke-RestMethod -Uri "http://127.0.0.1:8771/api/ingress/helper-readiness" `
        -Method Get -TimeoutSec 5
    Import-Module (Join-Path $paths.RuntimeRoot "PublicIngress.psm1") -Force -ErrorAction Stop
    $statePath = Join-Path $paths.RuntimeRoot "runtime\public-ingress-state.json"
    $state = Read-AfpPublicIngressState -Path $statePath -ExpectedCandidateId $ExpectedCandidateId
    if (
        -not [bool]$helper.ready -or
        [long]$helper.generation -ne [long]$state.generation -or
        [string]$helper.candidate_id -cne $ExpectedCandidateId
    ) {
        [pscustomobject]@{ status = "blocked"; error = "helper_readiness_gate_failed" } | ConvertTo-Json
        exit 5
    }
    $layers = @{}
    foreach ($property in $state.layers.PSObject.Properties) {
        $layers[$property.Name] = @{ ok = [bool]$property.Value.ok; code = [string]$property.Value.code }
    }
    $layers.ingress = @{ ok = $true; code = "ingress_committed" }
    $layers.helper = @{ ok = $true; code = "helper_ready" }
    $committed = Publish-AfpPublicIngressState -Path $statePath -PublicUrl ([string]$state.public_url) `
        -Provider ([string]$state.provider) -RecoveryOwner ([string]$state.recovery_owner) `
        -CandidateId ([string]$state.candidate_id) -Layers $layers `
        -ExpectedGeneration ([long]$state.generation)
    Complete-AfpRuntimePromotion -Promotion $pending.promotion -Paths $paths
    $result = [pscustomobject]@{
        status = "completed"
        candidate_id = $ExpectedCandidateId
        generation = [long]$committed.generation
        services = $services
        helper = $helper
    }
    $completedPath = Write-State "switchover-completed.json" $result
    [pscustomobject]@{ transaction = $result; evidence = $completedPath } | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -eq "StartupPreview") {
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    Get-AfpStartupRepairPreview -Inventory $inventory -Paths $paths | ConvertTo-Json -Depth 10
    exit 0
}

if ($Mode -eq "StartupApply") {
    if (-not $ApprovedInventoryDigest) { throw "StartupApply requires the approved preview digest." }
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    $preview = Get-AfpStartupRepairPreview -Inventory $inventory -Paths $paths
    Set-AfpApprovedStartupTargets -Preview $preview -PreviewDigest $ApprovedInventoryDigest | ConvertTo-Json -Depth 10
    exit 0
}

if ($Mode -eq "Evidence") {
    if (-not $EvidenceSource) { throw "Evidence mode requires EvidenceSource." }
    $destination = Join-Path $paths.EvidenceRoot (Split-Path -Leaf ([IO.Path]::GetFullPath($EvidenceSource).TrimEnd('\')))
    Copy-AfpEvidenceSafely -Source $EvidenceSource -Destination $destination -Paths $paths | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -eq "CleanupPreview") {
    if (-not $CleanupTargets.Count) { throw "CleanupPreview requires exact CleanupTargets." }
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    $preview = Get-AfpCleanupPreview -Targets $CleanupTargets -Paths $paths -Inventory $inventory -AllowedExternalTargets $legacyTargets
    $path = Write-State "cleanup-preview.json" $preview
    [pscustomobject]@{ preview = $preview; evidence = $path } | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -eq "Cleanup") {
    if (-not $PreviewPath) { $PreviewPath = Join-Path $evidenceDir "cleanup-preview.json" }
    if (-not (Test-Path -LiteralPath $PreviewPath -PathType Leaf)) { throw "Cleanup preview file is missing." }
    $preview = Get-Content -LiteralPath $PreviewPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    Remove-AfpApprovedTargets -Preview $preview -PreviewDigest $ApprovedInventoryDigest -Paths $paths -CurrentInventory $inventory | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -eq "Report") {
    $state = [pscustomobject]@{
        runtime_identity = $ExpectedCandidateId
        runtime_path = $paths.RuntimeRoot
        remaining_candidate_count = @($CleanupTargets | Where-Object { Test-Path -LiteralPath $_ }).Count
        released_bytes = 0
        evidence_directory = $paths.EvidenceRoot
        remaining_blockers = @()
        pairing = "blocked"
        simulation = "blocked"
        physical_sensor = "physical_sensor_pending"
    }
    $path = Write-AfpSingleRuntimeReport -State $state -OutputPath (Join-Path $evidenceDir "final-report.json") -Paths $paths
    [pscustomobject]@{ status = "passed"; report = $path } | ConvertTo-Json
    exit 0
}

throw "Mode $Mode requires the evidence/cleanup module implemented by the next task."
