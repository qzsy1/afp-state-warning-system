[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Source,
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
$candidateRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $candidateRoot "AFP_Integrated_System_Modular.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Candidate launcher is missing: $launcher"
}
$arguments = @("--pre-sensor-export-evidence", "--pre-sensor-output", $Output)
foreach ($item in $Source) {
    $arguments += @("--pre-sensor-evidence-source", $item)
}
& $launcher @arguments
exit $LASTEXITCODE
