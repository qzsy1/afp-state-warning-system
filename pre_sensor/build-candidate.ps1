[CmdletBinding()]
param(
    [string]$ReferenceRelease = "",
    [string]$ExistingExecutable = "",
    [string]$TargetRoot = "",
    [string]$HelperPythonExecutable = ""
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
if (-not $HelperPythonExecutable) {
    $repositoryPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $HelperPythonExecutable = if (Test-Path -LiteralPath $repositoryPython -PathType Leaf) {
        $repositoryPython
    } else {
        "py"
    }
}
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$helperOutput = Join-Path $temporaryRoot ("afp-candidate-helper-" + [guid]::NewGuid().ToString("N"))
try {
    $helperExecutable = & (Join-Path $RepoRoot "visualization_app\build_local_capture_helper.ps1") `
        -PythonExecutable $HelperPythonExecutable `
        -OutputDirectory $helperOutput
    $helperExecutable = [string]($helperExecutable | Select-Object -Last 1)
    if (-not (Test-Path -LiteralPath $helperExecutable -PathType Leaf)) {
        throw "Isolated Helper build did not produce an executable: $helperExecutable"
    }

    & (Join-Path $RepoRoot "modular_runtime\assemble_modular_delivery.ps1") `
        -ReferenceRelease $ReferenceRelease `
        -TargetDir $target `
        -ApplicationVersion $version `
        -LauncherVersion $version `
        -ExistingExecutable $ExistingExecutable `
        -HelperExecutable $helperExecutable `
        -ReleaseContract $contractPath `
        -CandidateId $candidateId `
        -SourceRevision $revision
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate assembly failed with exit code $LASTEXITCODE."
    }
} finally {
    $resolvedHelperOutput = [IO.Path]::GetFullPath($helperOutput)
    if ($resolvedHelperOutput.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedHelperOutput) -like "afp-candidate-helper-*") {
        Remove-Item -LiteralPath $resolvedHelperOutput -Recurse -Force -ErrorAction SilentlyContinue
    }
}
Write-Output $target
