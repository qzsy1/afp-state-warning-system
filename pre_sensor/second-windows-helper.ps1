# AFP second-Windows Helper lifecycle and readiness evidence entrypoint.
# This script never changes firewall, sleep, USB, adapter, proxy, or clock policy.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("install", "start", "stop", "restart", "check", "export")]
    [string]$Action,
    [string]$CandidateRoot = "",
    [string]$EvidenceDirectory = "",
    [string]$ServerUri = ""
)

$ErrorActionPreference = "Stop"
$taskName = "AFP Local Capture Helper"
if (-not $CandidateRoot) {
    $CandidateRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$CandidateRoot = [IO.Path]::GetFullPath($CandidateRoot)
if (-not $EvidenceDirectory) {
    $EvidenceDirectory = Join-Path $CandidateRoot "pre-sensor-evidence\second-windows"
}
$helperExecutable = Join-Path $CandidateRoot "local_helper\AFP_Local_Capture_Helper.exe"
$installer = Join-Path $CandidateRoot "install_local_helper_autostart.ps1"
$manifestPath = Join-Path $CandidateRoot "RELEASE-MANIFEST.json"
$sumsPath = Join-Path $CandidateRoot "SHA256SUMS.txt"
$reviewScript = Join-Path $CandidateRoot "review-windows-remediation.ps1"

function Get-TaskState {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { return "missing" }
    return [string]$task.State
}

function Test-CandidateHashes {
    if (-not (Test-Path -LiteralPath $sumsPath -PathType Leaf)) {
        return @{ ok = $false; checked = 0; error = "SHA256SUMS.txt missing" }
    }
    $checked = 0
    foreach ($line in Get-Content -LiteralPath $sumsPath -Encoding UTF8) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
            return @{ ok = $false; checked = $checked; error = "invalid checksum line" }
        }
        $expected = $Matches[1].ToLowerInvariant()
        $relative = $Matches[2].Replace('/', [IO.Path]::DirectorySeparatorChar)
        $target = [IO.Path]::GetFullPath((Join-Path $CandidateRoot $relative))
        if (-not $target.StartsWith($CandidateRoot, [StringComparison]::OrdinalIgnoreCase)) {
            return @{ ok = $false; checked = $checked; error = "checksum path escaped candidate" }
        }
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            return @{ ok = $false; checked = $checked; error = "managed file missing" }
        }
        $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            return @{ ok = $false; checked = $checked; error = "managed file digest mismatch" }
        }
        $checked++
    }
    return @{ ok = ($checked -gt 0); checked = $checked; error = $null }
}

function Get-SafeServerEvidence {
    if (-not $ServerUri) {
        return @{ status = "blocked"; reachable = $false; endpoint = $null; reason = "ServerUri not supplied" }
    }
    $uri = [Uri]$ServerUri
    if ($uri.UserInfo) { throw "ServerUri must not contain credentials" }
    $safeEndpoint = "{0}://{1}:{2}" -f $uri.Scheme, $uri.Host, $uri.Port
    try {
        $connection = Test-NetConnection -ComputerName $uri.Host -Port $uri.Port -WarningAction SilentlyContinue
        return @{ status = $(if ($connection.TcpTestSucceeded) { "passed" } else { "failed" }); reachable = [bool]$connection.TcpTestSucceeded; endpoint = $safeEndpoint; reason = $null }
    } catch {
        return @{ status = "blocked"; reachable = $false; endpoint = $safeEndpoint; reason = $_.Exception.GetType().Name }
    }
}

function New-ReadinessEvidence {
    $hashes = Test-CandidateHashes
    $network = Get-SafeServerEvidence
    $taskState = Get-TaskState
    $helperProcess = Get-Process -Name "AFP_Local_Capture_Helper" -ErrorAction SilentlyContinue
    $runtimeConfig = Join-Path $env:LOCALAPPDATA "AFP_Local_Capture_Helper\config.json"
    $checks = [ordered]@{
        release_manifest = Test-Path -LiteralPath $manifestPath -PathType Leaf
        managed_hashes = [bool]$hashes.ok
        helper_executable = Test-Path -LiteralPath $helperExecutable -PathType Leaf
        helper_task = $taskState -ne "missing"
        helper_process = $null -ne $helperProcess
        pairing_config_present = Test-Path -LiteralPath $runtimeConfig -PathType Leaf
        server_tcp = [bool]$network.reachable
        windows_review_available = Test-Path -LiteralPath $reviewScript -PathType Leaf
    }
    $failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
    $status = if ($failed.Count -eq 0) { "passed" } elseif ($network.status -eq "blocked") { "blocked" } else { "failed" }
    return [ordered]@{
        schema_version = 1
        evidence_tier = "two_machine_environment"
        host_role = "remote_helper"
        generated_at_utc = [DateTime]::UtcNow.ToString("o")
        status = $status
        checks = $checks
        failed_checks = $failed
        task_state = $taskState
        checksum_files_checked = [int]$hashes.checked
        checksum_error = $hashes.error
        server = $network
        physical_sensor = "not_exercised"
        note = "Run this on the actual second Windows computer; no firewall or power policy is changed."
    }
}

if ($Action -eq "install") {
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw "Helper installer is missing" }
    & $installer -StartNow
    exit $LASTEXITCODE
}
if ($Action -eq "start") {
    if ((Get-TaskState) -eq "missing") { throw "Helper scheduled task is not installed" }
    Start-ScheduledTask -TaskName $taskName
    exit 0
}
if ($Action -eq "stop") {
    if ((Get-TaskState) -eq "missing") { throw "Helper scheduled task is not installed" }
    Stop-ScheduledTask -TaskName $taskName
    exit 0
}
if ($Action -eq "restart") {
    if ((Get-TaskState) -eq "missing") { throw "Helper scheduled task is not installed" }
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $taskName
    exit 0
}

$evidence = New-ReadinessEvidence
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
$jsonPath = Join-Path $EvidenceDirectory "second-windows-helper-readiness.json"
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $jsonPath -Encoding UTF8
if ($Action -eq "export" -and (Test-Path -LiteralPath $reviewScript -PathType Leaf)) {
    & $reviewScript *> (Join-Path $EvidenceDirectory "windows-readiness-review.txt")
}
$evidence | ConvertTo-Json -Depth 8
$exitCode = if ($evidence.status -eq "passed") { 0 } elseif ($evidence.status -eq "blocked") { 2 } else { 1 }
exit $exitCode
