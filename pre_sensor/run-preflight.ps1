[CmdletBinding()]
param(
    [ValidateSet("local_direct", "remote_helper")]
    [string]$Mode = "local_direct",
    [string]$Config = "",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$candidateRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $candidateRoot "AFP_Integrated_System_Modular.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Candidate launcher is missing: $launcher"
}

$launcherArguments = @("--pre-sensor-preflight", "--pre-sensor-mode", $Mode)
if ($Config) {
    $launcherArguments += @("--pre-sensor-config", $Config)
}
if ($Output) {
    $launcherArguments += @("--pre-sensor-output", $Output)
}

& $launcher @launcherArguments
exit $LASTEXITCODE
