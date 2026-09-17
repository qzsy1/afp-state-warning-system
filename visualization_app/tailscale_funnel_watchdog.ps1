[CmdletBinding()]
param(
    [ValidateSet("Run", "Install", "Uninstall", "Once", "Status")]
    [string]$Mode = "Run"
)

$ErrorActionPreference = "SilentlyContinue"

$TaskName = "AFP Tailscale Funnel Watchdog"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValueName = "AFP_Tailscale_Funnel_Watchdog"
$LegacyTaskName = "AFP Public Tunnel Watchdog"
$LegacyRunValueName = "AFP_Public_Tunnel_Watchdog"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeliveryRoot = Join-Path $ProjectRoot "delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic"
if (-not (Test-Path (Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"))) {
    $DeliveryRoot = "F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic"
}
$AppExe = Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"
$HelperRoot = Join-Path $DeliveryRoot "local_helper"
$HelperExe = Join-Path $HelperRoot "AFP_Local_Capture_Helper.exe"
$FunnelRoot = "F:\softwawre\tailscale"
$FunnelUrlFile = Join-Path $FunnelRoot "funnel-url.txt"
$FunnelStatusFile = Join-Path $FunnelRoot "funnel-status.json"
$OriginUrl = "http://127.0.0.1:8770"
$FunnelArguments = "funnel --bg --https=443 --yes http://127.0.0.1:8770"

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

function Write-FunnelState([string]$Code, [string]$Message, [string]$PublicUrl = "") {
    New-Item -ItemType Directory -Path $FunnelRoot -Force | Out-Null
    $payload = [ordered]@{
        ok = ($Code -eq "online")
        code = $Code
        message = $Message
        public_url = $PublicUrl
        checked_at = (Get-Date).ToString("o")
    }
    $payload | ConvertTo-Json | Set-Content -LiteralPath $FunnelStatusFile -Encoding UTF8
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

function Test-PublicFunnel([string]$PublicUrl) {
    if (-not $PublicUrl) { return $false }
    $healthUrl = $PublicUrl.TrimEnd("/") + "/api/health"
    foreach ($attempt in 1..3) {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 12
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            if ($attempt -lt 3) { Start-Sleep -Seconds 2 }
        }
    }
    return $false
}

function Test-Funnel {
    # tailscale funnel status --json
    $result = Invoke-Tailscale @("funnel", "status", "--json")
    if ($result.ExitCode -ne 0 -or -not $result.Output) { return $false }
    return $result.Output -match "127\.0\.0\.1:8770|localhost:8770"
}

function Save-FunnelUrl([string]$Url) {
    if (-not $Url) { return $false }
    New-Item -ItemType Directory -Path $FunnelRoot -Force | Out-Null
    $normalized = $Url.TrimEnd("/") + "/"
    $previous = ""
    if (Test-Path -LiteralPath $FunnelUrlFile) {
        $previous = (Get-Content -LiteralPath $FunnelUrlFile -Raw).Trim()
    }
    Set-Content -LiteralPath $FunnelUrlFile -Value $normalized -Encoding ASCII
    return $previous -ne $normalized
}

function Ensure-Origin {
    if (Test-Origin) { return $true }
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
        Write-FunnelState "origin_executable_missing" "找不到系统主程序，未启动公网入口。"
        return $false
    }
    if (-not (Get-AppProcess)) {
        Start-Process -FilePath $AppExe -WorkingDirectory $DeliveryRoot -WindowStyle Hidden | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if (Test-Origin) { return $true }
        Start-Sleep -Seconds 2
    }
    Write-FunnelState "origin_unavailable" "本地系统未能在127.0.0.1:8770正常启动。"
    return $false
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
    $urlChanged = Save-FunnelUrl $publicUrl
    if ($urlChanged) { Restart-Helper | Out-Null } else { Ensure-Helper | Out-Null }
    Write-FunnelState "online" "固定公网地址、系统服务和辅助程序均已启动。" $publicUrl
    return $true
}

function Disable-LegacyQuickTunnel {
    Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $RunKey -Name $LegacyRunValueName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "cloudflared.exe" -and $_.CommandLine -match "127\.0\.0\.1:8770"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

function Install-Watchdog {
    if (-not (Ensure-Funnel)) { return $false }
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
    Disable-LegacyQuickTunnel
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
if ($Mode -eq "Status") {
    Ensure-Funnel | Out-Null
    if (Test-Path -LiteralPath $FunnelStatusFile) { Get-Content -LiteralPath $FunnelStatusFile -Raw }
    exit 0
}

while ($true) {
    Ensure-Funnel | Out-Null
    Start-Sleep -Seconds 20
}
