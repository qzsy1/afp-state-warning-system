[CmdletBinding()]
param(
    [string]$ReferenceRelease = "",
    [string]$ExistingExecutable = "",
    [string]$TargetRoot = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$contractPath = Join-Path $PSScriptRoot "release-contract.json"
$contract = Get-Content -LiteralPath $contractPath -Raw -Encoding UTF8 | ConvertFrom-Json
$version = [string]$contract.identity.application_version
$revision = (& git -C $RepoRoot rev-parse --short=12 HEAD).Trim()
if (-not $revision) { throw "Cannot resolve source revision." }
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$safeVersion = $version -replace '[^A-Za-z0-9._-]', '-'
$candidateId = "$safeVersion-$revision-$stamp"
if (-not $TargetRoot) {
    $TargetRoot = Join-Path $RepoRoot "artifacts\pre-sensor-candidates"
}
$target = Join-Path $TargetRoot $candidateId
if (Test-Path -LiteralPath $target) {
    throw "Target already exists; choose a new candidate id: $target"
}
if (-not $ReferenceRelease) {
    $releaseRoots = @(Get-ChildItem -LiteralPath (Join-Path $RepoRoot "delivery") -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName "AFP_Integrated_System_Modular.exe") -PathType Leaf
    })
    if ($releaseRoots.Count -ne 1) {
        throw "Expected exactly one reference release; pass -ReferenceRelease explicitly."
    }
    $ReferenceRelease = $releaseRoots[0].FullName
}
if (-not $ExistingExecutable) {
    $ExistingExecutable = Join-Path $ReferenceRelease "AFP_Integrated_System_Modular.exe"
}

& (Join-Path $RepoRoot "modular_runtime\assemble_modular_delivery.ps1") `
    -ReferenceRelease $ReferenceRelease `
    -TargetDir $target `
    -ApplicationVersion $version `
    -LauncherVersion $version `
    -ExistingExecutable $ExistingExecutable `
    -ReleaseContract $contractPath `
    -CandidateId $candidateId `
    -SourceRevision $revision
if ($LASTEXITCODE -ne 0) {
    throw "Candidate assembly failed with exit code $LASTEXITCODE."
}
Write-Output $target
