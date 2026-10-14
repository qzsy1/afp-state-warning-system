Set-StrictMode -Version Latest

$pathsModule = Join-Path $PSScriptRoot "SingleRuntimePaths.psm1"
Import-Module $pathsModule

function Get-AfpFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Test-AfpReleaseGate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$CandidatePath,
        [string]$ExpectedCandidateId = "",
        [scriptblock]$ExecutableCheckRunner
    )

    $root = [IO.Path]::GetFullPath($CandidatePath)
    $issues = [Collections.Generic.List[string]]::new()
    $checks = [Collections.Generic.List[object]]::new()
    foreach ($required in @("RELEASE-MANIFEST.json", "VERSION.json", "SHA256SUMS.txt")) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $required) -PathType Leaf)) {
            $issues.Add("required release file missing: $required")
        }
    }
    if ($issues.Count -gt 0) {
        return [pscustomobject]@{ status = "failed"; candidate_id = ""; source_revision = ""; checks = @(); issues = @($issues) }
    }

    try {
        $manifest = Get-Content -LiteralPath (Join-Path $root "RELEASE-MANIFEST.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        $version = Get-Content -LiteralPath (Join-Path $root "VERSION.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        $issues.Add("release metadata is unreadable")
        return [pscustomobject]@{ status = "failed"; candidate_id = ""; source_revision = ""; checks = @(); issues = @($issues) }
    }

    $candidateId = [string]$manifest.candidate.candidate_id
    $sourceRevision = [string]$manifest.candidate.source_revision
    if (-not $candidateId) { $issues.Add("candidate identity is missing") }
    if ($ExpectedCandidateId -and $candidateId -ne $ExpectedCandidateId) {
        $issues.Add("candidate identity mismatch")
    }
    $identityVersions = @(
        [string]$manifest.identity.application_version,
        [string]$manifest.identity.helper_version,
        [string]$manifest.identity.launcher_version,
        [string]$version.application_version,
        [string]$version.launcher_version
    )
    if ($identityVersions -contains "" -or @($identityVersions | Select-Object -Unique).Count -ne 1) {
        $issues.Add("release identity versions are missing or inconsistent")
    }
    $expectedContracts = [ordered]@{
        helper_protocol_version = 2
        unified_frame_contract = "unified_frame_v1"
        configuration_schema_version = 1
        readiness_schema_version = 1
    }
    foreach ($name in $expectedContracts.Keys) {
        if ($manifest.contracts.$name -ne $expectedContracts[$name]) {
            $issues.Add("release contract mismatch: $name")
        }
    }

    $hashes = @{}
    foreach ($line in (Get-Content -LiteralPath (Join-Path $root "SHA256SUMS.txt") -Encoding UTF8)) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9A-Fa-f]{64})\s+[ *](.+)$') {
            $issues.Add("SHA256SUMS line is invalid")
            continue
        }
        $relative = $Matches[2].Replace('/', '\')
        if ([IO.Path]::IsPathRooted($relative) -or $relative.Split('\') -contains '..') {
            $issues.Add("SHA256SUMS path escapes release")
            continue
        }
        $path = [IO.Path]::GetFullPath((Join-Path $root $relative))
        if (-not $path.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            $issues.Add("SHA256SUMS path escapes release")
        } elseif (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $issues.Add("SHA256SUMS file missing: $relative")
        } elseif ((Get-AfpFileSha256 -Path $path) -ne $Matches[1].ToLowerInvariant()) {
            $issues.Add("SHA256SUMS hash mismatch: $relative")
        } else {
            $hashes[$relative.ToLowerInvariant()] = $Matches[1].ToLowerInvariant()
        }
    }

    $declaredFiles = @()
    $declaredFiles += @($manifest.static_assets.required)
    $declaredFiles += @($manifest.configuration_templates.required)
    $declaredFiles += @($manifest.runtime_requirements.required_executables | ForEach-Object { $_.path })
    $declaredFiles += @($manifest.runtime_requirements.native_components | ForEach-Object { $_.path })
    foreach ($relativeValue in $declaredFiles) {
        $relative = [string]$relativeValue
        $path = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $issues.Add("declared release file missing: $relative")
            continue
        }
        if (-not $hashes.ContainsKey($relative.Replace('/', '\').ToLowerInvariant())) {
            $issues.Add("declared release file missing from SHA256SUMS: $relative")
        }
    }

    foreach ($entry in @($manifest.static_assets.sha256.PSObject.Properties)) {
        $path = Join-Path $root ([string]$entry.Name)
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and (Get-AfpFileSha256 -Path $path) -ne ([string]$entry.Value).ToLowerInvariant()) {
            $issues.Add("release static hash mismatch: $($entry.Name)")
        }
    }
    foreach ($entry in @($manifest.configuration_templates.sha256.PSObject.Properties)) {
        $path = Join-Path $root ([string]$entry.Name)
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and (Get-AfpFileSha256 -Path $path) -ne ([string]$entry.Value).ToLowerInvariant()) {
            $issues.Add("release configuration hash mismatch: $($entry.Name)")
        }
    }
    foreach ($item in @($manifest.runtime_requirements.required_executables) + @($manifest.runtime_requirements.native_components)) {
        $path = Join-Path $root ([string]$item.path)
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and (Get-AfpFileSha256 -Path $path) -ne ([string]$item.sha256).ToLowerInvariant()) {
            $issues.Add("release runtime hash mismatch: $($item.path)")
        }
    }

    $exe = Join-Path $root "AFP_Integrated_System_Modular.exe"
    foreach ($argument in @("--verify-files", "--self-test", "--module-status")) {
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            $result = [pscustomobject]@{ ExitCode = 127; Output = "launcher missing" }
        } elseif ($ExecutableCheckRunner) {
            $result = & $ExecutableCheckRunner $exe $argument
        } else {
            $output = (& $exe $argument 2>&1 | Out-String).Trim()
            $result = [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
        }
        $checks.Add([pscustomobject]@{ argument = $argument; exit_code = [int]$result.ExitCode })
        if ([int]$result.ExitCode -ne 0) {
            $issues.Add("release executable check failed: $argument")
        }
    }

    return [pscustomobject][ordered]@{
        status = if ($issues.Count -eq 0) { "passed" } else { "failed" }
        candidate_id = $candidateId
        source_revision = $sourceRevision
        checks = @($checks)
        issues = @($issues)
    }
}

function Resolve-AfpReferenceRelease {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [string]$RequestedPath = ""
    )

    if ($RequestedPath) {
        $resolved = [IO.Path]::GetFullPath($RequestedPath).TrimEnd('\')
        if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
            throw "Reference release does not exist: $resolved"
        }
        return $resolved
    }
    if (Test-Path -LiteralPath $Paths.RuntimeRoot -PathType Container) {
        return [IO.Path]::GetFullPath($Paths.RuntimeRoot).TrimEnd('\')
    }
    $deliveryRoot = Join-Path $Paths.SourceRoot "delivery"
    $matches = @(
        Get-ChildItem -LiteralPath $deliveryRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "AFP_Integrated_System_Modular*" }
    )
    if ($matches.Count -ne 1) {
        throw "Reference release is ambiguous or unavailable; specify ReferenceRelease explicitly."
    }
    return [IO.Path]::GetFullPath($matches[0].FullName).TrimEnd('\')
}

function Get-AfpDirectoryBytes {
    param([Parameter(Mandatory = $true)][string]$Path)
    $total = 0L
    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction Stop)) {
        $total += [long]$file.Length
    }
    return $total
}

function Test-AfpBuildReadiness {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [string]$GitRef = "HEAD",
        [string[]]$RequiredInputs = @(),
        [long]$RequiredBytes = 0,
        [string]$ReferenceRelease = "",
        [scriptblock]$FreeSpaceProvider
    )

    $issues = [Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
        $issues.Add("source repository is missing")
    } else {
        & git -C $RepoRoot rev-parse --verify "$GitRef^{commit}" *> $null
        if ($LASTEXITCODE -ne 0) { $issues.Add("Git rollback reference is unavailable") }
        & git -C $RepoRoot lfs env *> $null
        if ($LASTEXITCODE -ne 0) { $issues.Add("Git LFS is unavailable") }
    }
    foreach ($inputPath in $RequiredInputs) {
        if (-not (Test-Path -LiteralPath $inputPath)) {
            $issues.Add("required build input is missing")
        }
    }
    $requiredCapacity = $RequiredBytes
    if ($ReferenceRelease) {
        if (-not (Test-Path -LiteralPath $ReferenceRelease -PathType Container)) {
            $issues.Add("reference release is missing")
        } else {
            try {
                $referenceBytes = Get-AfpDirectoryBytes -Path $ReferenceRelease
                if ($referenceBytes -gt $requiredCapacity) { $requiredCapacity = $referenceBytes }
            } catch {
                $issues.Add("reference release size could not be determined")
            }
        }
    }
    $freeBytes = -1L
    try {
        if ($FreeSpaceProvider) {
            $freeBytes = [long](& $FreeSpaceProvider $Paths.WorkspaceRoot)
        } else {
            $driveName = ([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Paths.WorkspaceRoot))).TrimEnd('\').TrimEnd(':')
            $freeBytes = [long](Get-PSDrive -Name $driveName -ErrorAction Stop).Free
        }
        if ($requiredCapacity -gt 0 -and $freeBytes -lt $requiredCapacity) {
            $issues.Add("insufficient staging capacity")
        }
    } catch {
        $issues.Add("staging capacity could not be determined")
    }
    return [pscustomobject]@{
        status = if ($issues.Count -eq 0) { "passed" } else { "blocked" }
        issues = @($issues)
        required_bytes = $requiredCapacity
        free_bytes = $freeBytes
    }
}

function Remove-AfpDeploymentStaging {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StagingPath,
        [Parameter(Mandatory = $true)][psobject]$Paths
    )
    $target = Assert-AfpMutableTarget -Path $StagingPath -Paths $Paths -Kind Staging
    if (-not (Split-Path -Leaf $target).StartsWith("deployment-", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Staging cleanup target is not a deployment directory: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

function New-AfpStagedRelease {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)][string]$ReferenceRelease,
        [string]$ExistingExecutable = "",
        [string]$PythonExecutable = "",
        [string]$HelperExecutable = "",
        [string]$SourceRoot = ""
    )
    $script = Join-Path $Paths.SourceRoot "pre_sensor\build-candidate.ps1"
    $arguments = @{
        ReferenceRelease = $ReferenceRelease
        WorkspaceRoot = $Paths.WorkspaceRoot
    }
    if ($ExistingExecutable) { $arguments.ExistingExecutable = $ExistingExecutable }
    if ($PythonExecutable) { $arguments.PythonExecutable = $PythonExecutable }
    if ($HelperExecutable) { $arguments.HelperExecutable = $HelperExecutable }
    if ($SourceRoot) { $arguments.SourceRoot = $SourceRoot }
    $json = & $script @arguments
    if (-not $?) { throw "Staged release build failed." }
    return (($json -join "`n") | ConvertFrom-Json)
}

function New-AfpRollbackStage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)][string]$GitRef,
        [Parameter(Mandatory = $true)][string]$ReferenceRelease
    )
    $source = Join-Path $Paths.StagingRoot ("rollback-source-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $Paths.StagingRoot -Force | Out-Null
    $created = $false
    try {
        & git -C $Paths.SourceRoot worktree add --detach $source $GitRef *> $null
        if ($LASTEXITCODE -ne 0) { throw "Git rollback worktree could not be created." }
        $created = $true
        & git -C $source lfs pull *> $null
        if ($LASTEXITCODE -ne 0) { throw "Git LFS rollback inputs could not be materialized." }
        return New-AfpStagedRelease -Paths $Paths -ReferenceRelease $ReferenceRelease -SourceRoot $source
    } finally {
        if ($created) {
            & git -C $Paths.SourceRoot worktree remove --force $source *> $null
        }
        if (Test-Path -LiteralPath $source) {
            $resolved = Assert-AfpMutableTarget -Path $source -Paths $Paths -Kind Staging
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}

Export-ModuleMember -Function @(
    "Resolve-AfpReferenceRelease",
    "New-AfpStagedRelease",
    "New-AfpRollbackStage",
    "Test-AfpReleaseGate",
    "Test-AfpBuildReadiness",
    "Remove-AfpDeploymentStaging"
)
