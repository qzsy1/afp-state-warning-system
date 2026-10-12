# AFP Windows readiness remediation review
# Review only: this script reads state and never changes firewall or power policy.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Write-Host "Review only - no system setting will be changed."
Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction
netsh winhttp show proxy
powercfg /getactivescheme
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
powercfg /query SCHEME_CURRENT SUB_USB USBSELECTIVE
Get-NetAdapterPowerManagement -ErrorAction Continue | Format-Table -AutoSize
Get-Service W32Time | Select-Object Name, Status, StartType
w32tm /query /status
Write-Host "Any suggested change requires separate administrator approval."
