[CmdletBinding()]
param(
    [ValidateSet("Run", "Install", "Uninstall", "Once")]
    [string]$Mode = "Run"
)

$ErrorActionPreference = "SilentlyContinue"

$TaskName = "AFP Public Tunnel Watchdog"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValueName = "AFP_Public_Tunnel_Watchdog"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeliveryRoot = Join-Path $ProjectRoot "delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic"
if (-not (Test-Path (Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"))) {
    $DeliveryRoot = "F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic"
}
$AppExe = Join-Path $DeliveryRoot "AFP_Integrated_System_Modular.exe"
$HelperRoot = Join-Path $DeliveryRoot "local_helper"
$HelperExe = Join-Path $HelperRoot "AFP_Local_Capture_Helper.exe"
$CloudflaredExe = "F:\softwawre\cloudflared\cloudflared.exe"
$CloudflaredRoot = Split-Path -Parent $CloudflaredExe
$TunnelLog = Join-Path $CloudflaredRoot "quick-tunnel-watchdog.log"
$TunnelUrlFile = Join-Path $CloudflaredRoot "quick-tunnel-url.txt"
$OriginUrl = "http://127.0.0.1:8770"
$MetricsUrl = "http://127.0.0.1:20241/metrics"

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
    $healthUrl = $PublicUrl.TrimEnd("/") + "/api/health"
    foreach ($attempt in 1..2) {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 8
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            if ($attempt -lt 2) { Start-Sleep -Seconds 2 }
        }
    }
    return $false
}

function Save-TunnelUrl([string]$Url) {
    if (-not $Url) { return $false }
    Set-Content -LiteralPath $TunnelUrlFile -Value ($Url.TrimEnd("/") + "/") -Encoding ASCII
    return $true
}

function Test-Tunnel {
    $metrics = Get-TunnelMetrics
    if (-not $metrics) { return $false }
    $match = [regex]::Match($metrics, 'cloudflared_tunnel_ha_connections\s+(\d+)')
    if (-not $match.Success -or [int]$match.Groups[1].Value -lt 1) { return $false }
    $url = Get-TunnelUrl $metrics
    if (-not $url) { return $false }
    if (-not (Test-PublicTunnel $url)) { return $false }
    return [bool](Save-TunnelUrl $url)
}

function Ensure-Origin {
    if (Test-Origin) { return $true }
    if (-not (Test-Path $AppExe)) { return $false }
    if (-not (Get-AppProcess)) {
        Start-Process -FilePath $AppExe -WorkingDirectory $DeliveryRoot -WindowStyle Hidden | Out-Null
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
        Stop-Process -Id $process.ProcessId -Force
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
    return $false
}

function Ensure-Tunnel {
    if (-not (Ensure-Origin)) { return $false }
    if (Test-Tunnel) {
        Ensure-Helper | Out-Null
        return $true
    }
    Stop-StaleTunnel
    $started = Start-Tunnel
    if ($started) { Restart-Helper | Out-Null }
    return $started
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
    Ensure-Tunnel | Out-Null
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
    Ensure-Tunnel | Out-Null
    exit 0
}

while ($true) {
    Ensure-Tunnel | Out-Null
    Start-Sleep -Seconds 20
}
