[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Inventory", "Stage", "Switchover", "StartupPreview", "StartupApply", "Evidence", "CleanupPreview", "Cleanup", "Report", "Rollback")]
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
    [string]$PreviewPath = ""
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
    "D:\AFP-Main-Evidence\CROSS-20261013-01",
    "F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic"
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
    $oldProcesses = @(Stop-AfpManagedRuntimeProcesses -Inventory $inventory -RuntimeExecutable $exe)
    $promotion = Invoke-AfpRuntimePromotion -StagingPath $StagingPath -Paths $paths -ApprovedInventoryDigest $ApprovedInventoryDigest -CurrentInventory $inventory
    $restartPlan = if ($oldProcesses.Count) {
        $oldProcesses
    } else {
        @([pscustomobject]@{ pid = 0; launch_arguments = "--server-only" })
    }
    Start-AfpManagedRuntimeProcesses -Processes $restartPlan -RuntimeExecutable $exe -WorkingDirectory $paths.RuntimeRoot | Out-Null
    $services = Wait-AfpRuntimeServices -RuntimeRoot $paths.RuntimeRoot -PublicUrl $PublicUrl -LanIPv4 $LanIPv4 -ExpectedCandidateId $ExpectedCandidateId -FunnelOrigin $FunnelOrigin -TimeoutSeconds 90 -PollIntervalMilliseconds 1000
    if ($services.status -ne "passed") {
        $failedInventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
        Stop-AfpManagedRuntimeProcesses -Inventory $failedInventory -RuntimeExecutable $exe | Out-Null
        Restore-AfpRuntimePromotion -Promotion $promotion -Paths $paths
        if ($oldProcesses.Count) {
            Start-AfpManagedRuntimeProcesses -Processes $oldProcesses -RuntimeExecutable $exe -WorkingDirectory $paths.RuntimeRoot | Out-Null
        }
        $services | ConvertTo-Json -Depth 10
        exit 4
    }
    Complete-AfpRuntimePromotion -Promotion $promotion -Paths $paths
    [pscustomobject]@{ status = "service_verified"; promotion = $promotion; services = $services } | ConvertTo-Json -Depth 12
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
