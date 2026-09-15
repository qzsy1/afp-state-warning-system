param(
    [switch]$Uninstall,
    [switch]$StartNow
)

# Usage: .\install_local_helper_autostart.ps1 -StartNow / -Uninstall

$ErrorActionPreference = "Stop"
$taskName = "AFP Local Capture Helper"
$projectRoot = Split-Path -Parent $PSScriptRoot
$helperDirectory = Join-Path $projectRoot "delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic\local_helper"
if (-not (Test-Path -LiteralPath $helperDirectory -PathType Container)) {
    # The same script is copied beside the helper in the delivery folder.
    $helperDirectory = Join-Path $PSScriptRoot "local_helper"
}
$helperExecutable = Join-Path $helperDirectory "AFP_Local_Capture_Helper.exe"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除本地采集辅助程序自动启动任务。"
    exit 0
}

if (-not (Test-Path -LiteralPath $helperExecutable -PathType Leaf)) {
    throw "找不到辅助程序：$helperExecutable"
}

$action = New-ScheduledTaskAction -Execute $helperExecutable -WorkingDirectory $helperDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "保持 AFP 本地采集辅助程序登录后在线；网络异常由程序自身自动重连。" `
    -Force | Out-Null

if ($StartNow) {
    Start-ScheduledTask -TaskName $taskName
}
Write-Host "已配置本地采集辅助程序自动启动：$helperExecutable"
