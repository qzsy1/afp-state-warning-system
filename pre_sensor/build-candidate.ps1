[CmdletBinding()]
param(
    [string]$ReferenceRelease = "",
    [string]$ExistingExecutable = "",
    [string]$TargetRoot = "",
    [string]$WorkspaceRoot = "",
    [string]$PythonExecutable = "",
    [string]$HelperExecutable = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$pathsModule = Join-Path $PSScriptRoot "SingleRuntimePaths.psm1"
$pathConfig = Join-Path $PSScriptRoot "single-runtime-paths.json"
Import-Module $pathsModule -Force
$paths = if ($WorkspaceRoot) {
    Get-AfpDeploymentPaths -ConfigPath $pathConfig -WorkspaceRootOverride $WorkspaceRoot
} else {
    Get-AfpDeploymentPaths -ConfigPath $pathConfig
}
$contractPath = Join-Path $PSScriptRoot "release-contract.json"
$contract = Get-Content -LiteralPath $contractPath -Raw -Encoding UTF8 | ConvertFrom-Json
$version = [string]$contract.identity.application_version
$gitRevision = (& git -C $RepoRoot rev-parse --short=12 HEAD).Trim()
if (-not $gitRevision) { throw "Cannot resolve source revision." }
$revision = $gitRevision
$candidateRevision = $gitRevision
$diff = (& git -C $RepoRoot diff --binary HEAD -- .) -join "`n"
if ($diff) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $diffHash = ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($diff)))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
    $revision = "$gitRevision+working-tree.$($diffHash.Substring(0, 12))"
    $candidateRevision = "$gitRevision-wt$($diffHash.Substring(0, 8))"
}
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$safeVersion = $version -replace '[^A-Za-z0-9._-]', '-'
$safeRevision = $candidateRevision -replace '[^A-Za-z0-9._-]', '-'
$candidateId = "$safeVersion-$safeRevision-$stamp"
if (-not $TargetRoot) {
    $TargetRoot = $paths.StagingRoot
}
$resolvedTargetRoot = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
if (-not [string]::Equals($resolvedTargetRoot, $paths.StagingRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "TargetRoot must equal the configured .afp-staging root: $resolvedTargetRoot"
}
$target = Join-Path $resolvedTargetRoot ("deployment-" + [guid]::NewGuid().ToString("N"))
if (Test-Path -LiteralPath $target) {
    throw "Target already exists; choose a new deployment id: $target"
}
if (-not $ReferenceRelease) {
    if (Test-Path -LiteralPath (Join-Path $paths.RuntimeRoot "AFP_Integrated_System_Modular.exe") -PathType Leaf) {
        $ReferenceRelease = $paths.RuntimeRoot
    } else {
        $releaseRoots = @(Get-ChildItem -LiteralPath (Join-Path $RepoRoot "delivery") -Directory | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName "AFP_Integrated_System_Modular.exe") -PathType Leaf
        })
        if ($releaseRoots.Count -ne 1) {
            throw "Expected exactly one reference release; pass -ReferenceRelease explicitly."
        }
        $ReferenceRelease = $releaseRoots[0].FullName
    }
}
if (-not $ExistingExecutable) {
    $ExistingExecutable = Join-Path $ReferenceRelease "AFP_Integrated_System_Modular.exe"
}
if (-not $PythonExecutable) {
    $repositoryPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $PythonExecutable = if (Test-Path -LiteralPath $repositoryPython -PathType Leaf) {
        $repositoryPython
    } else {
        "py"
    }
}
$helperBuildRoot = ""
$buildSucceeded = $false
try {
    New-Item -ItemType Directory -Path $resolvedTargetRoot -Force | Out-Null
    if (-not $HelperExecutable) {
        $helperBuildRoot = Join-Path $resolvedTargetRoot ("helper-" + [guid]::NewGuid().ToString("N"))
        $HelperExecutable = Join-Path $helperBuildRoot "AFP_Local_Capture_Helper.exe"
        & (Join-Path $RepoRoot "visualization_app\build_local_capture_helper.ps1") `
            -PythonExecutable $PythonExecutable `
            -OutputDirectory $helperBuildRoot | Out-Null
    }
    $HelperExecutable = [IO.Path]::GetFullPath($HelperExecutable)
    if (-not (Test-Path -LiteralPath $HelperExecutable -PathType Leaf)) {
        throw "Helper build input does not exist: $HelperExecutable"
    }
    & (Join-Path $RepoRoot "modular_runtime\assemble_modular_delivery.ps1") `
        -ReferenceRelease $ReferenceRelease `
        -TargetDir $target `
        -ApplicationVersion $version `
        -LauncherVersion $version `
        -ExistingExecutable $ExistingExecutable `
        -ModelsSource (Join-Path $RepoRoot "visualization_app\models") `
        -DataSource (Join-Path $RepoRoot "visualization_app\data") `
        -NewCollectionSource (Join-Path $RepoRoot "visualization_app\new_collection_demo_v11_3") `
        -ModelRuntimeSource (Join-Path $RepoRoot "visualization_app\model_runtime") `
        -SoftConsistencySource (Join-Path $RepoRoot "outputs_tc_hi_soft_consistency_v13_8") `
        -CausalConsistencySource (Join-Path $RepoRoot "outputs_causal_online_consistency_v13_9") `
        -HelperExecutable $HelperExecutable `
        -ReleaseContract $contractPath `
        -CandidateId $candidateId `
        -SourceRevision $revision
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate assembly failed with exit code $LASTEXITCODE."
    }
    $buildSucceeded = $true
}
finally {
    if ($helperBuildRoot) {
        $resolvedHelperBuildRoot = [IO.Path]::GetFullPath($helperBuildRoot)
        if ($resolvedHelperBuildRoot.StartsWith($paths.StagingRoot + '\', [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedHelperBuildRoot).StartsWith("helper-", [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $resolvedHelperBuildRoot)) {
            Remove-Item -LiteralPath $resolvedHelperBuildRoot -Recurse -Force
        }
    }
    if (-not $buildSucceeded -and (Test-Path -LiteralPath $target)) {
        $safeTarget = Assert-AfpMutableTarget -Path $target -Paths $paths -Kind Staging
        Remove-Item -LiteralPath $safeTarget -Recurse -Force
    }
}
[pscustomobject][ordered]@{
    status = "staged"
    deployment_id = Split-Path -Leaf $target
    staging_path = $target
    candidate_id = $candidateId
    source_revision = $revision
} | ConvertTo-Json -Compress
