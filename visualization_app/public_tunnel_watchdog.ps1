[CmdletBinding()]
param(
    [ValidateSet("Run", "Install", "Uninstall", "Once", "Cutover")]
    [string]$Mode = "Run",
    [long]$ExpectedGeneration = -1,
    [switch]$ApproveOwnerCutover
)

$ErrorActionPreference = "SilentlyContinue"

$TaskName = "AFP Public Tunnel Watchdog"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValueName = "AFP_Public_Tunnel_Watchdog"
$DeliveryRoot = $PSScriptRoot
if (-not (Test-Path (Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"))) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    $WorkspaceRoot = Split-Path -Parent $ProjectRoot
    $DeliveryRoot = Join-Path $WorkspaceRoot "AFP_Integrated_System_Modular"
}
$AppExe = Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"
$HelperRoot = Join-Path $DeliveryRoot "local_helper"
$HelperExe = Join-Path $HelperRoot "AFP_Local_Capture_Helper.exe"
$CloudflaredExe = "F:\softwawre\cloudflared\cloudflared.exe"
$CloudflaredRoot = Split-Path -Parent $CloudflaredExe
$TunnelLog = Join-Path $CloudflaredRoot "quick-tunnel-watchdog.log"
$TunnelUrlFile = Join-Path $CloudflaredRoot "quick-tunnel-url.txt"
$OriginUrl = "http://127.0.0.1:8770"
$ControlUrl = "http://127.0.0.1:8771"
$MetricsUrl = "http://127.0.0.1:20241/metrics"
$TunnelStatusFile = Join-Path $CloudflaredRoot "quick-tunnel-status.json"
$IngressModule = Join-Path $PSScriptRoot "PublicIngress.psm1"
Import-Module $IngressModule -Force -ErrorAction Stop
$IngressStateFile = Join-Path $DeliveryRoot "runtime\public-ingress-state.json"
$ReleaseManifestFile = Join-Path $DeliveryRoot "RELEASE-MANIFEST.json"
$script:CutoverReservationId = ""

function New-WatchdogLayer([bool]$Ok, [string]$Code) {
    return [ordered]@{ ok = $Ok; code = $Code }
}

function Write-TunnelState([string]$Code, [string]$PublicUrl = "", $Layers = $null) {
    if (-not $Layers) {
        $Layers = @{
            origin = New-WatchdogLayer $false $Code
            control_plane = New-WatchdogLayer $false $Code
            dns = New-WatchdogLayer $false $Code
            public_edge = New-WatchdogLayer $false $Code
            ingress = New-WatchdogLayer $false $Code
            helper = New-WatchdogLayer $false $Code
        }
    }
    $allReady = $true
    foreach ($layer in $Layers.Values) {
        if (-not [bool]$layer.ok) { $allReady = $false }
    }
    [ordered]@{
        overall_status = if ($allReady) { "online" } else { "failed" }
        origin = $Layers.origin
        control_plane = $Layers.control_plane
        dns = $Layers.dns
        public_edge = $Layers.public_edge
        ingress = $Layers.ingress
        helper = $Layers.helper
        public_url = $PublicUrl
        non_sensitive_error = if ($allReady) { "" } else { $Code }
        checked_at = (Get-Date).ToString("o")
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $TunnelStatusFile -Encoding UTF8
    Release-CutoverReservation
}

function Get-CandidateId {
    try {
        return [string](Get-Content -LiteralPath $ReleaseManifestFile -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop).candidate.candidate_id
    } catch { return "" }
}

function Get-CurrentIngressState {
    try { return Read-AfpPublicIngressState -Path $IngressStateFile -ExpectedCandidateId (Get-CandidateId) }
    catch { return $null }
}

function Get-CutoverReadiness {
    try {
        $result = Invoke-RestMethod -Uri ($ControlUrl + "/api/ingress/cutover-reservation") `
            -Method Post -ContentType "application/json" -Body '{"ttl_seconds":300}' -TimeoutSec 5
        $script:CutoverReservationId = [string]$result.reservation_id
        return $result
    } catch {
        return [pscustomobject]@{ allowed = $false; error = "cutover_readiness_unavailable" }
    }
}

function Release-CutoverReservation {
    if (-not $script:CutoverReservationId) { return }
    try {
        $body = @{ reservation_id = $script:CutoverReservationId } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri ($ControlUrl + "/api/ingress/cutover-release") `
            -Method Post -ContentType "application/json" -Body $body -TimeoutSec 5 | Out-Null
    } catch {
    } finally {
        $script:CutoverReservationId = ""
    }
}

function Wait-HelperIngressReady([long]$Generation, [string]$CandidateId) {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        try {
            $status = Invoke-RestMethod -Uri ($ControlUrl + "/api/ingress/helper-readiness") `
                -Method Get -TimeoutSec 5
            if ([bool]$status.ready -and [long]$status.generation -eq $Generation -and
                [string]$status.candidate_id -ceq $CandidateId) { return $true }
        } catch {
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-WatchdogMutationAllowed {
    param($State, [string]$Provider, [string]$RequestedMode, [long]$RequiredGeneration, [bool]$Approved, $Readiness)
    $owner = if ($State) { [string]$State.recovery_owner } else { "" }
    $generation = if ($State) { [long]$State.generation } else { 0L }
    if ($RequestedMode -eq "Install") { return [pscustomobject]@{ allowed = $false; reason = "install_observe_only" } }
    if ($RequestedMode -eq "Cutover") {
        if (-not $Approved) { return [pscustomobject]@{ allowed = $false; reason = "owner_cutover_not_approved" } }
        if ($RequiredGeneration -lt 0 -or $RequiredGeneration -ne $generation) {
            return [pscustomobject]@{ allowed = $false; reason = "ingress_generation_mismatch" }
        }
        if (-not $Readiness -or -not [bool]$Readiness.allowed) {
            return [pscustomobject]@{ allowed = $false; reason = "active_capture_blocks_cutover" }
        }
        return [pscustomobject]@{ allowed = $true; reason = "approved_cutover" }
    }
    if ($owner -and $owner.Trim().ToLowerInvariant() -eq $Provider.Trim().ToLowerInvariant()) {
        if (-not $Readiness -or -not [bool]$Readiness.allowed) {
            return [pscustomobject]@{ allowed = $false; reason = "active_capture_blocks_cutover" }
        }
        return [pscustomobject]@{ allowed = $true; reason = "current_owner" }
    }
    return [pscustomobject]@{ allowed = $false; reason = "non_owner_observe_only" }
}

function ConvertTo-WatchdogLayerTable($Layers) {
    $table = @{}
    if ($Layers) {
        foreach ($property in $Layers.PSObject.Properties) {
            $table[$property.Name] = @{
                ok = [bool]$property.Value.ok
                code = [string]$property.Value.code
            }
        }
    }
    return $table
}

function Restore-PreviousIngressState(
    $PreviousState,
    [long]$PublishedGeneration,
    [string]$CandidateUrl,
    [string]$CandidateId
) {
    try {
        if (-not $PreviousState) {
            $failedLayers = @{
                origin = New-WatchdogLayer $false "rollback_no_previous_ingress"
                control_plane = New-WatchdogLayer $false "rollback_no_previous_ingress"
                dns = New-WatchdogLayer $false "rollback_no_previous_ingress"
                public_edge = New-WatchdogLayer $false "rollback_no_previous_ingress"
                ingress = New-WatchdogLayer $false "rollback_no_previous_ingress"
                helper = New-WatchdogLayer $false "rollback_no_previous_ingress"
            }
            Publish-AfpPublicIngressState -Path $IngressStateFile `
                -PublicUrl $CandidateUrl -Provider "cloudflare" -RecoveryOwner "cloudflare" `
                -CandidateId $CandidateId -Layers $failedLayers `
                -ExpectedGeneration $PublishedGeneration `
                -MinimumGeneration ($PublishedGeneration + 1L) | Out-Null
            return $true
        }
        $layers = ConvertTo-WatchdogLayerTable $PreviousState.layers
        $layers["helper"] = New-WatchdogLayer $false "rollback_helper_reconciliation_required"
        Publish-AfpPublicIngressState -Path $IngressStateFile `
            -PublicUrl ([string]$PreviousState.public_url) `
            -Provider ([string]$PreviousState.provider) `
            -RecoveryOwner ([string]$PreviousState.recovery_owner) `
            -CandidateId ([string]$PreviousState.candidate_id) `
            -Layers $layers -ExpectedGeneration $PublishedGeneration `
            -MinimumGeneration ($PublishedGeneration + 1L) | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Stop-CandidateIngress {
    try {
        Stop-StaleTunnel
        return $true
    } catch {
        return $false
    }
}

function Retire-PreviousIngress {
    param(
        [string]$Provider,
        [scriptblock]$TailscaleRetirer = $null
    )
    if (-not $Provider -or $Provider.Trim().ToLowerInvariant() -eq "cloudflare") {
        return $true
    }
    if ($Provider.Trim().ToLowerInvariant() -ne "tailscale") {
        return $false
    }
    if ($TailscaleRetirer) {
        return [bool](& $TailscaleRetirer)
    }
    try {
        $command = Get-Command tailscale.exe -ErrorAction Stop
        & $command.Source funnel reset 2>&1 | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Save-TunnelUrlAtomic([string]$Url) {
    $temporary = $TunnelUrlFile + "." + [Guid]::NewGuid().ToString("N") + ".tmp"
    try {
        Set-Content -LiteralPath $temporary -Value ($Url.TrimEnd("/") + "/") -Encoding ASCII
        if (Test-Path -LiteralPath $TunnelUrlFile -PathType Leaf) {
            [IO.File]::Replace($temporary, $TunnelUrlFile, $null, $true)
        } else {
            [IO.File]::Move($temporary, $TunnelUrlFile)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Get-AppProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "AFP_Integrated_System_Modular.exe" -and
        $_.ExecutablePath -eq $AppExe
    }
}

function Get-TunnelProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "cloudflared.exe" -and
        $_.ExecutablePath -eq $CloudflaredExe -and
        $_.CommandLine -match "127\.0\.0\.1:8770"
    }
}

function Get-HelperProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "AFP_Local_Capture_Helper.exe" -and
        $_.ExecutablePath -eq $HelperExe
    }
}

function Test-Origin {
    try {
        $response = Invoke-WebRequest -Uri ($OriginUrl + "/api/health") -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-TunnelMetrics {
    try {
        return (Invoke-WebRequest -Uri $MetricsUrl -UseBasicParsing -TimeoutSec 5).Content
    } catch {
        return ""
    }
}

function Get-TunnelUrl([string]$Metrics) {
    $match = [regex]::Match($Metrics, 'userHostname="(https://[a-z0-9-]+\.trycloudflare\.com)"')
    if ($match.Success) {
        return $match.Groups[1].Value
    }
    return ""
}

function Test-PublicTunnel([string]$PublicUrl) {
    if (-not $PublicUrl) { return $false }
    try { $healthUri = [Uri]($PublicUrl.TrimEnd("/") + "/api/health") }
    catch { return $false }
    if ($healthUri.Scheme -ne "https") { return $false }
    try { $addresses = @(Resolve-AfpPublicEdgeAddresses -HostName $healthUri.DnsSafeHost) }
    catch { return $false }
    foreach ($address in $addresses) {
        try {
            if ((Invoke-AfpForcedPublicProbe -Uri $healthUri -Address $address).ok) { return $true }
        } catch {
        }
    }
    return $false
}

function Test-Tunnel {
    $metrics = Get-TunnelMetrics
    if (-not $metrics) { return $false }
    $match = [regex]::Match($metrics, 'cloudflared_tunnel_ha_connections\s+(\d+)')
    if (-not $match.Success -or [int]$match.Groups[1].Value -lt 1) { return $false }
    $url = Get-TunnelUrl $metrics
    if (-not $url) { return $false }
    if (-not (Test-PublicTunnel $url)) { return $false }
    return $true
}

function Ensure-Origin {
    if (Test-Origin) { return $true }
    if (-not (Test-Path $AppExe)) { return $false }
    if (-not (Get-AppProcess)) {
        Start-Process -FilePath $AppExe `
            -ArgumentList "--server-only" `
            -WorkingDirectory $DeliveryRoot `
            -WindowStyle Hidden | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if (Test-Origin) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Stop-StaleTunnel {
    $processes = @(Get-TunnelProcess)
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }
    if ($processes.Count) { Start-Sleep -Seconds 2 }
}

function Ensure-Helper {
    if (-not (Test-Path -LiteralPath $HelperExe -PathType Leaf)) { return $false }
    if (Get-HelperProcess) { return $true }
    Start-Process -FilePath $HelperExe `
        -ArgumentList "--background" `
        -WorkingDirectory $HelperRoot `
        -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (Get-HelperProcess) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Restart-Helper {
    $processes = @(Get-HelperProcess)
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force
    }
    if ($processes.Count) { Start-Sleep -Seconds 1 }
    return Ensure-Helper
}

function Start-Tunnel {
    if (-not (Test-Path $CloudflaredExe)) { return $false }
    Start-Process -FilePath $CloudflaredExe `
        -ArgumentList "tunnel --url $OriginUrl --no-autoupdate --protocol http2" `
        -WorkingDirectory $CloudflaredRoot `
        -RedirectStandardError $TunnelLog `
        -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-Tunnel) { return $true }
        Start-Sleep -Seconds 2
    }
    try { Stop-StaleTunnel } catch {
    }
    return $false
}

function Ensure-Tunnel {
    $state = Get-CurrentIngressState
    if (-not (Ensure-Origin)) {
        Write-TunnelState "origin_unavailable"
        return $false
    }
    $readiness = Get-CutoverReadiness
    $decision = Test-WatchdogMutationAllowed $state "cloudflare" $Mode $ExpectedGeneration ([bool]$ApproveOwnerCutover) $readiness
    if (-not $decision.allowed) {
        Write-TunnelState ([string]$decision.reason)
        return $false
    }
    $previousProvider = if ($state) { [string]$state.provider } else { "" }
    $crossProviderCutover = $Mode -eq "Cutover" -and $previousProvider -and
        $previousProvider.Trim().ToLowerInvariant() -ne "cloudflare"
    $candidateStarted = $false
    if (-not (Test-Tunnel)) {
        Stop-StaleTunnel
        if (-not (Start-Tunnel)) {
            Write-TunnelState "control_plane_unavailable"
            return $false
        }
        $candidateStarted = $true
    }
    $publicUrl = Get-TunnelUrl (Get-TunnelMetrics)
    if (-not $publicUrl -or -not (Test-PublicTunnel $publicUrl)) {
        if ($candidateStarted -or $crossProviderCutover) { Stop-CandidateIngress | Out-Null }
        Write-TunnelState "public_edge_unavailable" $publicUrl
        return $false
    }
    if ($state -and [string]$state.public_url -ne $publicUrl.TrimEnd("/") -and -not [bool]$readiness.allowed) {
        Write-TunnelState "active_capture_blocks_cutover" $publicUrl
        return $false
    }
    $layers = @{
        origin = New-WatchdogLayer $true "origin_ready"
        control_plane = New-WatchdogLayer $true "cloudflare_running"
        dns = New-WatchdogLayer $true "public_dns_ready"
        public_edge = New-WatchdogLayer $true "public_edge_200"
        ingress = New-WatchdogLayer $false "ingress_not_published"
        helper = New-WatchdogLayer $false "helper_not_checked"
    }
    try {
        $candidateId = Get-CandidateId
        if (-not $candidateId) { throw "candidate_identity_unavailable" }
        $publishParameters = @{
            Path = $IngressStateFile
            PublicUrl = $publicUrl
            Provider = "cloudflare"
            RecoveryOwner = "cloudflare"
            CandidateId = $candidateId
            Layers = $layers
        }
        if ($state) { $publishParameters.ExpectedGeneration = [long]$state.generation }
        elseif ($ExpectedGeneration -ge 0) { $publishParameters.ExpectedGeneration = $ExpectedGeneration }
        $publication = Publish-AfpPublicIngressState @publishParameters
        $layers.ingress = New-WatchdogLayer $true "ingress_published"
    } catch {
        if ($candidateStarted -or $crossProviderCutover) { Stop-CandidateIngress | Out-Null }
        Write-TunnelState "ingress_publication_failed" $publicUrl $layers
        return $false
    }
    $helperProcessReady = if ($publication.changed) { Restart-Helper } else { Ensure-Helper }
    $helperReady = $helperProcessReady -and (
        Wait-HelperIngressReady ([long]$publication.generation) $candidateId
    )
    $layers.helper = if ($helperReady) {
        New-WatchdogLayer $true "helper_ready"
    } else {
        New-WatchdogLayer $false "helper_recovery_failed"
    }
    if (-not $helperReady) {
        $rolledBack = if ($publication.changed) {
            Restore-PreviousIngressState $state ([long]$publication.generation) $publicUrl $candidateId
        } else { $true }
        if ($candidateStarted -or $crossProviderCutover) { Stop-CandidateIngress | Out-Null }
        $code = if ($rolledBack) { "helper_recovery_failed" } else { "cutover_rollback_failed" }
        Write-TunnelState $code $publicUrl $layers
        return $false
    }
    try {
        Publish-AfpPublicIngressState -Path $IngressStateFile -PublicUrl $publicUrl `
            -Provider "cloudflare" -RecoveryOwner "cloudflare" -CandidateId $candidateId `
            -Layers $layers -ExpectedGeneration ([long]$publication.generation) | Out-Null
    } catch {
        $rolledBack = if ($publication.changed) {
            Restore-PreviousIngressState $state ([long]$publication.generation) $publicUrl $candidateId
        } else { $true }
        if ($candidateStarted -or $crossProviderCutover) { Stop-CandidateIngress | Out-Null }
        $code = if ($rolledBack) { "ingress_publication_failed" } else { "cutover_rollback_failed" }
        Write-TunnelState $code $publicUrl $layers
        return $false
    }
    if ($crossProviderCutover -and -not (Retire-PreviousIngress $previousProvider)) {
        if (-not (Test-PublicTunnel ([string]$state.public_url))) {
            $layers.ingress = New-WatchdogLayer $false "previous_ingress_compensation_unavailable"
            try {
                Publish-AfpPublicIngressState -Path $IngressStateFile -PublicUrl $publicUrl `
                    -Provider "cloudflare" -RecoveryOwner "cloudflare" -CandidateId $candidateId `
                    -Layers $layers -ExpectedGeneration ([long]$publication.generation) | Out-Null
            } catch {
            }
            Write-TunnelState "cutover_rollback_failed" $publicUrl $layers
            return $false
        }
        $rolledBack = Restore-PreviousIngressState $state ([long]$publication.generation) $publicUrl $candidateId
        $candidateStopped = Stop-CandidateIngress
        $code = if ($rolledBack -and $candidateStopped) {
            "previous_ingress_retirement_failed"
        } else { "cutover_rollback_failed" }
        Write-TunnelState $code $publicUrl $layers
        return $false
    }
    Save-TunnelUrlAtomic $publicUrl
    Write-TunnelState "online" $publicUrl $layers
    return $true
}

function Install-Watchdog {
    $scriptPath = $PSCommandPath
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Mode Run"
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
        (New-ScheduledTaskTrigger -AtStartup)
    )
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited
    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $triggers `
            -Settings $settings `
            -Principal $principal `
            -Force `
            -ErrorAction Stop | Out-Null
    } catch {
        # Some managed Windows accounts cannot create a root scheduled task.
        # HKCU Run is per-user, needs no elevation, and survives sleep/logon.
        New-Item -Path $RunKey -Force | Out-Null
        Set-ItemProperty -Path $RunKey -Name $RunValueName -Value ($powershell + " " + $arguments)
    }
    return $true
}

if ($Mode -eq "Install") {
    Install-Watchdog
    exit 0
}
if ($Mode -eq "Uninstall") {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction SilentlyContinue
    exit 0
}
if ($Mode -eq "Once") {
    if (Ensure-Tunnel) { exit 0 } else { exit 2 }
}
if ($Mode -eq "Cutover") {
    if (Ensure-Tunnel) { exit 0 } else { exit 2 }
}

while ($true) {
    Ensure-Tunnel | Out-Null
    Start-Sleep -Seconds 20
}
