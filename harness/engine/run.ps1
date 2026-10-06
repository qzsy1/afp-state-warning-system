[CmdletBinding()]
param(
    [ValidateSet("quick", "full", "release")][string]$Profile,
    [string]$Check,
    [switch]$List,
    [switch]$EnvironmentCheck,
    [switch]$Setup,
    [ValidateSet("local", "ci")][string]$Environment = "local",
    [string]$BaseRef,
    [string[]]$ChangedFile = @(),
    [string]$BaselineExe,
    [string]$BaselineExeSha256,
    [string]$Matrix = "harness/config/regression-matrix.json",
    [string]$Profiles = "harness/config/profiles.json",
    [string]$ExeRules = "harness/config/exe-rebuild-rules.json",
    [string]$LogRoot = "harness/logs",
    [string]$ReportDir = "harness/reports",
    [switch]$OpenReport
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$settingsPath = Join-Path $repoRoot "harness\config\local-settings.json"
$configuredPython = $null
if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
    try {
        $settings = Get-Content -Raw -LiteralPath $settingsPath -Encoding UTF8 | ConvertFrom-Json
        $configuredPython = $settings.python_path
    } catch {
        Write-Error "Invalid harness local settings: $($_.Exception.Message)"
        exit 2
    }
}

$candidates = @()
if ($env:AFP_PYTHON) { $candidates += $env:AFP_PYTHON }
if ($configuredPython) {
    if ([IO.Path]::IsPathRooted($configuredPython)) { $candidates += $configuredPython }
    else { $candidates += (Join-Path $repoRoot $configuredPython) }
}
$candidates += (Join-Path $repoRoot ".venv\Scripts\python.exe")
$candidates += "py"
$candidates += "python"
$python = $null
$prefix = @()
foreach ($candidate in $candidates) {
    if ($candidate -eq "py") {
        if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py"; $prefix = @("-3.11"); break }
    } elseif ((Test-Path -LiteralPath $candidate -PathType Leaf) -or (Get-Command $candidate -ErrorAction SilentlyContinue)) {
        $python = $candidate; break
    }
}
if (-not $python) {
    Write-Error "Python 3.11 was not found. Create .venv or set AFP_PYTHON."
    exit 2
}

$arguments = @(
    "-m", "harness.engine.harness_cli", "--environment", $Environment,
    "--matrix", $Matrix, "--profiles", $Profiles, "--exe-rules", $ExeRules,
    "--log-root", $LogRoot, "--report-dir", $ReportDir
)
if ($OpenReport) { $arguments += "--open-report" }
if ($BaseRef) { $arguments += @("--base-ref", $BaseRef) }
foreach ($item in $ChangedFile) { $arguments += @("--changed-file", $item) }
if ($BaselineExe) { $arguments += @("--baseline-exe", $BaselineExe) }
if ($BaselineExeSha256) { $arguments += @("--baseline-exe-sha256", $BaselineExeSha256) }
if ($Setup) { $arguments += "setup" }
elseif ($EnvironmentCheck) { $arguments += "environment" }
elseif ($List) { $arguments += "list" }
elseif ($Check) { $arguments += @("check", $Check) }
elseif ($Profile) { $arguments += @("profile", $Profile) }
else { Write-Error "Specify -Profile, -Check, -List, -EnvironmentCheck, or -Setup."; exit 2 }

Push-Location $repoRoot
try {
    & $python @prefix @arguments
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
