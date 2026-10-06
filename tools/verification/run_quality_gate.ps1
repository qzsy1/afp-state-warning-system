[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("quick", "full", "release")]
    [string]$Profile,
    [ValidateSet("local", "ci")][string]$Environment = "local",
    [string]$Matrix = "harness/config/regression-matrix.json",
    [string]$ExeRules = "harness/config/exe-rebuild-rules.json",
    [string]$BaseRef,
    [string[]]$ChangedFile = @(),
    [string]$BaselineExe,
    [string]$BaselineExeSha256,
    [string]$ReportDir = "harness/reports"
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$entrypoint = Join-Path $repoRoot "harness\engine\run.ps1"
$arguments = @{
    Profile = $Profile
    Environment = $Environment
    Matrix = $Matrix
    ExeRules = $ExeRules
    ReportDir = $ReportDir
}
if ($BaseRef) { $arguments.BaseRef = $BaseRef }
if ($ChangedFile) { $arguments.ChangedFile = $ChangedFile }
if ($BaselineExe) { $arguments.BaselineExe = $BaselineExe }
if ($BaselineExeSha256) { $arguments.BaselineExeSha256 = $BaselineExeSha256 }
& $entrypoint @arguments
exit $LASTEXITCODE
