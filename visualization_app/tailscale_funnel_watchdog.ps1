[CmdletBinding()]
param(
    [ValidateSet("Run", "Install", "Uninstall", "Once", "Status", "Cutover")]
    [string]$Mode = "Run",
    [long]$ExpectedGeneration = -1,
    [switch]$ApproveOwnerCutover
)

$ErrorActionPreference = "SilentlyContinue"

$TaskName = "AFP Tailscale Funnel Watchdog"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValueName = "AFP_Tailscale_Funnel_Watchdog"
$DeliveryRoot = $PSScriptRoot
if (-not (Test-Path (Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"))) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    $WorkspaceRoot = Split-Path -Parent $ProjectRoot
    $DeliveryRoot = Join-Path $WorkspaceRoot "AFP_Integrated_System_Modular"
}
$AppExe = Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"
$HelperRoot = Join-Path $DeliveryRoot "local_helper"
$HelperExe = Join-Path $HelperRoot "AFP_Local_Capture_Helper.exe"
$FunnelRoot = "F:\softwawre\tailscale"
$FunnelUrlFile = Join-Path $FunnelRoot "funnel-url.txt"
$FunnelStatusFile = Join-Path $FunnelRoot "funnel-status.json"
$OriginUrl = "http://127.0.0.1:8770"
$FunnelArguments = "funnel --bg --https=443 --yes http://127.0.0.1:8770"
$OriginLaunchMutexName = "Local\AFP_Tailscale_Funnel_Watchdog_Origin_Start"
$IngressModule = Join-Path $PSScriptRoot "PublicIngress.psm1"
Import-Module $IngressModule -Force -ErrorAction Stop
$IngressStateFile = Join-Path $DeliveryRoot "runtime\public-ingress-state.json"
$ReleaseManifestFile = Join-Path $DeliveryRoot "RELEASE-MANIFEST.json"

function Find-TailscaleExe {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Tailscale\tailscale.exe")
    )
    $command = Get-Command "tailscale.exe" -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    return ""
}

$TailscaleExe = Find-TailscaleExe

function New-WatchdogLayer([bool]$Ok, [string]$Code) {
    return [ordered]@{ ok = $Ok; code = $Code }
}

function Write-FunnelState([string]$Code, [string]$Message, [string]$PublicUrl = "", $Layers = $null) {
    New-Item -ItemType Directory -Path $FunnelRoot -Force | Out-Null
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
    $payload = [ordered]@{
        ok = $allReady
        code = $Code
        message = $Message
        public_url = $PublicUrl
        overall_status = if ($allReady) { "online" } else { "failed" }
        origin = $Layers.origin
        control_plane = $Layers.control_plane
        dns = $Layers.dns
        public_edge = $Layers.public_edge
        ingress = $Layers.ingress
        helper = $Layers.helper
        non_sensitive_error = if ($allReady) { "" } else { $Code }
        checked_at = (Get-Date).ToString("o")
    }
    $payload | ConvertTo-Json | Set-Content -LiteralPath $FunnelStatusFile -Encoding UTF8
}

function Get-CandidateId {
    try {
        return [string](Get-Content -LiteralPath $ReleaseManifestFile -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop).candidate.candidate_id
    } catch {
        return ""
    }
}

function Get-CurrentIngressState {
    try { return Read-AfpPublicIngressState -Path $IngressStateFile -ExpectedCandidateId (Get-CandidateId) }
    catch { return $null }
}

function Get-CutoverReadiness {
    try {
        return Invoke-RestMethod -Uri ($OriginUrl + "/api/ingress/cutover-readiness") `
            -Method Get -TimeoutSec 5
    } catch {
        return [pscustomobject]@{ allowed = $false; error = "cutover_readiness_unavailable" }
    }
}

function Test-WatchdogMutationAllowed {
    param($State, [string]$Provider, [string]$RequestedMode, [long]$RequiredGeneration, [bool]$Approved, $Readiness)
    $owner = if ($State) { [string]$State.recovery_owner } else { "" }
    $generation = if ($State) { [long]$State.generation } else { 0L }
    if ($RequestedMode -eq "Install") {
        return [pscustomobject]@{ allowed = $false; reason = "install_observe_only" }
    }
    if ($owner -and $owner.Trim().ToLowerInvariant() -eq $Provider.Trim().ToLowerInvariant()) {
        return [pscustomobject]@{ allowed = $true; reason = "current_owner" }
    }
    if ($RequestedMode -ne "Cutover") {
        return [pscustomobject]@{ allowed = $false; reason = "non_owner_observe_only" }
    }
    if (-not $Approved) {
        return [pscustomobject]@{ allowed = $false; reason = "owner_cutover_not_approved" }
    }
    if ($RequiredGeneration -lt 0 -or $RequiredGeneration -ne $generation) {
        return [pscustomobject]@{ allowed = $false; reason = "ingress_generation_mismatch" }
    }
    if (-not $Readiness -or -not [bool]$Readiness.allowed) {
        return [pscustomobject]@{ allowed = $false; reason = "active_capture_blocks_cutover" }
    }
    return [pscustomobject]@{ allowed = $true; reason = "approved_cutover" }
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

function Restore-PreviousIngressState($PreviousState, [long]$PublishedGeneration) {
    try {
        if (-not $PreviousState) {
            Remove-Item -LiteralPath $IngressStateFile -Force -ErrorAction Stop
            return -not (Test-Path -LiteralPath $IngressStateFile -PathType Leaf)
        }
        $layers = ConvertTo-WatchdogLayerTable $PreviousState.layers
        Publish-AfpPublicIngressState -Path $IngressStateFile `
            -PublicUrl ([string]$PreviousState.public_url) `
            -Provider ([string]$PreviousState.provider) `
            -RecoveryOwner ([string]$PreviousState.recovery_owner) `
            -CandidateId ([string]$PreviousState.candidate_id) `
            -Layers $layers -ExpectedGeneration $PublishedGeneration | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Stop-CandidateIngress {
    $result = Invoke-TailscaleWithTimeout @("funnel", "reset")
    return $result.ExitCode -eq 0
}

function Retire-PreviousIngress {
    param(
        [string]$Provider,
        [scriptblock]$CloudflareRetirer = $null
    )
    if (-not $Provider -or $Provider.Trim().ToLowerInvariant() -eq "tailscale") {
        return $true
    }
    if ($Provider.Trim().ToLowerInvariant() -ne "cloudflare") {
        return $false
    }
    if ($CloudflareRetirer) {
        return [bool](& $CloudflareRetirer)
    }
    try {
        $processes = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq "cloudflared.exe" -and $_.CommandLine -match "127\.0\.0\.1:8770"
        })
        foreach ($process in $processes) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        }
        return $true
    } catch {
        return $false
    }
}

function Save-FunnelUrlAtomic([string]$Url) {
    New-Item -ItemType Directory -Path $FunnelRoot -Force | Out-Null
    $temporary = $FunnelUrlFile + "." + [Guid]::NewGuid().ToString("N") + ".tmp"
    try {
        Set-Content -LiteralPath $temporary -Value ($Url.TrimEnd("/") + "/") -Encoding ASCII
        if (Test-Path -LiteralPath $FunnelUrlFile -PathType Leaf) {
            [IO.File]::Replace($temporary, $FunnelUrlFile, $null, $true)
        } else {
            [IO.File]::Move($temporary, $FunnelUrlFile)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Tailscale([string[]]$Arguments) {
    if (-not $TailscaleExe) {
        return [pscustomobject]@{ ExitCode = 127; Output = "tailscale_not_installed" }
    }
    $output = (& $TailscaleExe @Arguments 2>&1 | Out-String).Trim()
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
}

function Invoke-TailscaleWithTimeout([string[]]$Arguments, [int]$TimeoutSeconds = 20) {
    if (-not $TailscaleExe) {
        return [pscustomobject]@{ ExitCode = 127; Output = "tailscale_not_installed" }
    }
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $TailscaleExe `
            -ArgumentList $Arguments `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -WindowStyle Hidden `
            -PassThru
        $finished = $process.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)
        if (-not $finished) {
            Stop-Process -Id $process.Id -Force
            $process.WaitForExit()
        }
        $output = @(
            (Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue),
            (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
        ) -join "`n"
        $exitCode = if ($finished) { $process.ExitCode } else { 124 }
        return [pscustomobject]@{ ExitCode = $exitCode; Output = $output.Trim() }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-TailscaleState {
    # tailscale status --json
    $result = Invoke-Tailscale @("status", "--json")
    if ($result.ExitCode -ne 0 -or -not $result.Output) { return $null }
    try { return ($result.Output | ConvertFrom-Json) } catch { return $null }
}

function Get-PublicUrl {
    # Stable public form: https://device.tailnet-name.ts.net
    $state = Get-TailscaleState
    if (-not $state -or [string]$state.BackendState -ne "Running") { return "" }
    $dnsName = [string]$state.Self.DNSName
    if (-not $dnsName -or $dnsName -notmatch "\.ts\.net\.?$") { return "" }
    return "https://" + $dnsName.TrimEnd(".")
}

function Get-AppProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "AFP_Integrated_System_Modular.exe" -and
        $_.ExecutablePath -eq $AppExe
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

function Test-OriginListener {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort 8770 -ErrorAction SilentlyContinue
    )
    return $listeners.Count -gt 0
}

function Test-PublicFunnel {
    param(
        [Parameter(Position = 0)]
        [string]$PublicUrl,
        [Parameter(Position = 1)]
        [scriptblock]$ProbeRunner = $null
    )
    if (-not $PublicUrl) { return $false }
    try {
        $healthUri = [Uri]($PublicUrl.TrimEnd("/") + "/api/health")
    } catch {
        return $false
    }
    if ($healthUri.Scheme -ne "https") { return $false }
    $addresses = @(Resolve-AfpPublicEdgeAddresses -HostName $healthUri.DnsSafeHost)
    if (-not $addresses.Count) { return $false }
    foreach ($attempt in 1..3) {
        foreach ($address in $addresses) {
            if ($ProbeRunner) {
                if (& $ProbeRunner $healthUri $address) { return $true }
            } else {
                $result = Invoke-AfpForcedPublicProbe -Uri $healthUri -Address $address
                if ($result.ok) { return $true }
            }
        }
        if ($attempt -lt 3) { Start-Sleep -Seconds 2 }
    }
    return $false
}

function Test-Funnel {
    # tailscale funnel status --json
    $result = Invoke-Tailscale @("funnel", "status", "--json")
    if ($result.ExitCode -ne 0 -or -not $result.Output) { return $false }
    return $result.Output -match "127\.0\.0\.1:8770|localhost:8770"
}

function Ensure-Origin {
    if (Test-Origin) { return $true }
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
        Write-FunnelState "origin_executable_missing" "找不到系统主程序，未启动公网入口。"
        return $false
    }
    $launchMutex = New-Object System.Threading.Mutex($false, $OriginLaunchMutexName)
    $mutexHeld = $false
    try {
        try {
            $mutexHeld = $launchMutex.WaitOne([TimeSpan]::FromSeconds(60))
        } catch [System.Threading.AbandonedMutexException] {
            $mutexHeld = $true
        }
        if (-not $mutexHeld) {
            Write-FunnelState "origin_recovery_busy" "本地系统恢复操作仍在进行。"
            return $false
        }
        if (Test-Origin) { return $true }
        if (-not (Test-OriginListener)) {
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
        Write-FunnelState "origin_unavailable" "本地系统未能在127.0.0.1:8770正常启动。"
        return $false
    } finally {
        if ($mutexHeld) {
            $launchMutex.ReleaseMutex()
        }
        $launchMutex.Dispose()
    }
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

function Start-Funnel {
    # Keep this literal command visible for release audits:
    # tailscale funnel --bg --https=443 --yes http://127.0.0.1:8770
    $result = Invoke-TailscaleWithTimeout @("funnel", "--bg", "--https=443", "--yes", "http://127.0.0.1:8770")
    if ($result.ExitCode -ne 0) {
        Write-FunnelState "funnel_approval_required" ("Tailscale Funnel尚未获准：" + $result.Output)
        return $false
    }
    return $true
}

function Ensure-Funnel {
    $state = Get-CurrentIngressState
    $readiness = Get-CutoverReadiness
    $decision = Test-WatchdogMutationAllowed $state "tailscale" $Mode $ExpectedGeneration ([bool]$ApproveOwnerCutover) $readiness
    if (-not $decision.allowed) {
        Write-FunnelState ([string]$decision.reason) "当前 watchdog 不是公网入口恢复所有者，仅报告状态。"
        return $false
    }
    $previousProvider = if ($state) { [string]$state.provider } else { "" }
    $crossProviderCutover = $Mode -eq "Cutover" -and $previousProvider -and
        $previousProvider.Trim().ToLowerInvariant() -ne "tailscale"
    if (-not $TailscaleExe) {
        Write-FunnelState "tailscale_not_installed" "尚未安装Tailscale。"
        return $false
    }
    if (-not (Ensure-Origin)) { return $false }
    $publicUrl = Get-PublicUrl
    if (-not $publicUrl) {
        Write-FunnelState "tailscale_login_required" "Tailscale尚未登录，需先在服务器电脑完成一次账号登录。"
        return $false
    }
    if ($state -and [string]$state.public_url -ne $publicUrl.TrimEnd("/") -and -not [bool]$readiness.allowed) {
        Write-FunnelState "active_capture_blocks_cutover" "活动采集期间不得切换公网地址。" $publicUrl
        return $false
    }
    if (-not (Test-Funnel)) {
        if (-not (Start-Funnel)) { return $false }
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            if (Test-Funnel) { break }
            Start-Sleep -Seconds 2
        }
    }
    if (-not (Test-Funnel)) {
        Write-FunnelState "funnel_unavailable" "Tailscale已登录，但Funnel没有指向本地8770端口。" $publicUrl
        return $false
    }
    if (-not (Test-PublicFunnel $publicUrl)) {
        Write-FunnelState "public_health_failed" "固定公网地址已生成，但公网健康检查未通过。" $publicUrl
        return $false
    }
    $layers = @{
        origin = New-WatchdogLayer $true "origin_ready"
        control_plane = New-WatchdogLayer $true "tailscale_running"
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
            Provider = "tailscale"
            RecoveryOwner = "tailscale"
            CandidateId = $candidateId
            Layers = $layers
        }
        if ($state) { $publishParameters.ExpectedGeneration = [long]$state.generation }
        elseif ($ExpectedGeneration -ge 0) { $publishParameters.ExpectedGeneration = $ExpectedGeneration }
        $publication = Publish-AfpPublicIngressState @publishParameters
        $layers.ingress = New-WatchdogLayer $true "ingress_published"
    } catch {
        if ($crossProviderCutover) { Stop-CandidateIngress | Out-Null }
        Write-FunnelState "ingress_publication_failed" "公网入口状态发布失败。" $publicUrl $layers
        return $false
    }
    $helperReady = if ($publication.changed) { Restart-Helper } else { Ensure-Helper }
    $layers.helper = if ($helperReady) {
        New-WatchdogLayer $true "helper_ready"
    } else {
        New-WatchdogLayer $false "helper_recovery_failed"
    }
    if (-not $helperReady) {
        $rolledBack = if ($publication.changed) {
            Restore-PreviousIngressState $state ([long]$publication.generation)
        } else { $true }
        if ($crossProviderCutover) { Stop-CandidateIngress | Out-Null }
        $code = if ($rolledBack) { "helper_recovery_failed" } else { "cutover_rollback_failed" }
        Write-FunnelState $code "公网边缘可用，但辅助程序未就绪。" $publicUrl $layers
        return $false
    }
    try {
        Publish-AfpPublicIngressState -Path $IngressStateFile -PublicUrl $publicUrl `
            -Provider "tailscale" -RecoveryOwner "tailscale" -CandidateId $candidateId `
            -Layers $layers -ExpectedGeneration ([long]$publication.generation) | Out-Null
    } catch {
        $rolledBack = if ($publication.changed) {
            Restore-PreviousIngressState $state ([long]$publication.generation)
        } else { $true }
        if ($crossProviderCutover) { Stop-CandidateIngress | Out-Null }
        $code = if ($rolledBack) { "ingress_publication_failed" } else { "cutover_rollback_failed" }
        Write-FunnelState $code "公网入口最终状态发布失败。" $publicUrl $layers
        return $false
    }
    if ($crossProviderCutover -and -not (Retire-PreviousIngress $previousProvider)) {
        Write-FunnelState "previous_ingress_retirement_failed" "新入口已就绪，但旧入口需要人工清理。" $publicUrl $layers
        return $false
    }
    Save-FunnelUrlAtomic $publicUrl
    Write-FunnelState "online" "固定公网地址、系统服务和辅助程序均已启动。" $publicUrl $layers
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
        New-Item -Path $RunKey -Force | Out-Null
        Set-ItemProperty -Path $RunKey -Name $RunValueName -Value ($powershell + " " + $arguments)
    }
    return $true
}

if ($Mode -eq "Install") {
    if (Install-Watchdog) { exit 0 } else { exit 2 }
}
if ($Mode -eq "Uninstall") {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction SilentlyContinue
    exit 0
}
if ($Mode -eq "Once") {
    if (Ensure-Funnel) { exit 0 } else { exit 2 }
}
if ($Mode -eq "Cutover") {
    if (Ensure-Funnel) { exit 0 } else { exit 2 }
}
if ($Mode -eq "Status") {
    Ensure-Funnel | Out-Null
    if (Test-Path -LiteralPath $FunnelStatusFile) { Get-Content -LiteralPath $FunnelStatusFile -Raw }
    exit 0
}

while ($true) {
    Ensure-Funnel | Out-Null
    Start-Sleep -Seconds 20
}
