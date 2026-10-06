[CmdletBinding()]
param(
    [string]$HelperPath = "C:\AFP-Lab\Work\helper\AFP_Local_Capture_Helper.exe",
    [ValidateRange(1, 60)][int]$DurationSeconds = 10
)

$ErrorActionPreference = "Stop"
$resolvedHelper = [System.IO.Path]::GetFullPath($HelperPath)
if (-not (Test-Path -LiteralPath $resolvedHelper -PathType Leaf)) {
    throw "Helper executable is missing: $resolvedHelper"
}

$ruleName = "AFP-Client-Lab-Helper-Block-$PID"
try {
    New-NetFirewallRule -DisplayName $ruleName -Direction Outbound -Program $resolvedHelper -Action Block -Profile Any | Out-Null
    Start-Sleep -Seconds $DurationSeconds
}
finally {
    Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
}

[ordered]@{
    schema = "afp-client-lab-network-interruption-v1"
    helper = [System.IO.Path]::GetFileName($resolvedHelper)
    duration_seconds = $DurationSeconds
    firewall_rule_removed = $true
} | ConvertTo-Json -Depth 3
