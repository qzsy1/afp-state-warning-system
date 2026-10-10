[CmdletBinding()]
param(
    [ValidateSet("local_direct", "remote_helper")]
    [string]$Mode = "local_direct",
    [double]$DurationSeconds = 1800.0,
    [string]$Output = "",
    [switch]$FaultDrill
)

$ErrorActionPreference = "Stop"
$candidateRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $candidateRoot "AFP_Integrated_System_Modular.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Candidate launcher is missing: $launcher"
}
if ($DurationSeconds -le 0) {
    throw "DurationSeconds must be positive"
}
$arguments = @(
    "--pre-sensor-endurance",
    "--pre-sensor-mode", $Mode,
    "--pre-sensor-duration", ([string]$DurationSeconds)
)
if ($Output) {
    $arguments += @("--pre-sensor-output", $Output)
}
if ($FaultDrill) {
    $arguments += "--pre-sensor-fault-drill"
}
& $launcher @arguments
exit $LASTEXITCODE
