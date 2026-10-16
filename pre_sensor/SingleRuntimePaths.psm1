Set-StrictMode -Version Latest

function Normalize-AfpPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Path must not be empty."
    }
    if ([Management.Automation.WildcardPattern]::ContainsWildcardCharacters($Path)) {
        throw "Wildcards are not allowed in mutable paths."
    }
    if ($Path -match '%[^%]+%' -or $Path -match '\$\{?[^\\/]+') {
        throw "Unresolved environment or shell variables are not allowed in mutable paths."
    }
    return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
}

function Resolve-AfpPhysicalPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Depth = 0
    )

    if ($Depth -gt 32) { throw "Too many reparse-point expansions in mutable path." }
    $normalized = Normalize-AfpPath -Path $Path
    $root = [IO.Path]::GetPathRoot($normalized)
    if (-not $root) { throw "Mutable path has no filesystem root: $normalized" }
    $current = $root
    $relative = $normalized.Substring($root.Length)
    foreach ($segment in @($relative -split '[\\/]' | Where-Object { $_ })) {
        $next = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $next)) {
            $current = $next
            continue
        }
        $item = Get-Item -LiteralPath $next -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -and $item.Target) {
            $target = [string](@($item.Target)[0])
            if (-not [IO.Path]::IsPathRooted($target)) {
                $target = Join-Path $item.Parent.FullName $target
            }
            $current = Resolve-AfpPhysicalPath -Path $target -Depth ($Depth + 1)
        } else {
            $current = [string]$item.FullName
        }
    }
    return Normalize-AfpPath -Path $current
}

function Test-AfpSamePath {
    param([string]$Left, [string]$Right)
    return [string]::Equals(
        (Normalize-AfpPath -Path $Left),
        (Normalize-AfpPath -Path $Right),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Test-AfpDescendantPath {
    param([string]$Path, [string]$Parent)
    $candidate = (Normalize-AfpPath -Path $Path) + [IO.Path]::DirectorySeparatorChar
    $root = (Normalize-AfpPath -Path $Parent) + [IO.Path]::DirectorySeparatorChar
    return $candidate.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
}

function Get-AfpDeploymentPaths {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [string]$WorkspaceRootOverride = ""
    )

    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($WorkspaceRootOverride) {
        $workspace = Normalize-AfpPath -Path $WorkspaceRootOverride
        $source = Join-Path $workspace "AFP-Readiness-Repo"
        $runtime = Join-Path $workspace "AFP_Integrated_System_Modular"
        $evidence = Join-Path $workspace "AFP-Main-Evidence"
        $staging = Join-Path $workspace ".afp-staging"
    } else {
        $workspace = Normalize-AfpPath -Path ([string]$config.workspace_root)
        $source = Normalize-AfpPath -Path ([string]$config.source_root)
        $runtime = Normalize-AfpPath -Path ([string]$config.runtime_root)
        $evidence = Normalize-AfpPath -Path ([string]$config.evidence_root)
        $staging = Normalize-AfpPath -Path ([string]$config.staging_root)
    }

    foreach ($child in @($source, $runtime, $evidence, $staging)) {
        if (-not (Test-AfpDescendantPath -Path $child -Parent $workspace)) {
            throw "Configured path is outside the AFP workspace: $child"
        }
    }

    return [pscustomobject][ordered]@{
        SchemaVersion = [int]$config.schema_version
        WorkspaceRoot = $workspace
        SourceRoot = Normalize-AfpPath -Path $source
        RuntimeRoot = Normalize-AfpPath -Path $runtime
        EvidenceRoot = Normalize-AfpPath -Path $evidence
        StagingRoot = Normalize-AfpPath -Path $staging
    }
}

function Resolve-AfpSafePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot
    )

    $workspace = Resolve-AfpPhysicalPath -Path $WorkspaceRoot
    $candidate = Resolve-AfpPhysicalPath -Path $Path
    if (-not (Test-AfpSamePath -Left $candidate -Right $workspace) -and
        -not (Test-AfpDescendantPath -Path $candidate -Parent $workspace)) {
        throw "Mutable path is outside the AFP workspace: $candidate"
    }
    return $candidate
}

function Test-AfpProtectedTarget {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][psobject]$Paths
    )

    $candidate = Normalize-AfpPath -Path $Path
    $protected = @(
        $Paths.WorkspaceRoot,
        $Paths.SourceRoot,
        $Paths.RuntimeRoot,
        $Paths.EvidenceRoot,
        $Paths.StagingRoot
    )
    foreach ($item in $protected) {
        if ((Test-AfpSamePath -Left $candidate -Right $item) -or
            (Test-AfpDescendantPath -Path $item -Parent $candidate)) {
            return $true
        }
    }
    return $false
}

function Assert-AfpMutableTarget {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)]
        [ValidateSet("Staging", "Runtime", "Evidence", "Cleanup")]
        [string]$Kind
    )

    $candidate = Resolve-AfpSafePath -Path $Path -WorkspaceRoot $Paths.WorkspaceRoot
    switch ($Kind) {
        "Staging" {
            if (-not (Test-AfpDescendantPath -Path $candidate -Parent $Paths.StagingRoot)) {
                throw "Staging target must be a child of the staging root: $candidate"
            }
        }
        "Runtime" {
            if (-not (Test-AfpSamePath -Left $candidate -Right $Paths.RuntimeRoot)) {
                throw "Runtime target must equal the fixed runtime root: $candidate"
            }
        }
        "Evidence" {
            if (-not (Test-AfpSamePath -Left $candidate -Right $Paths.EvidenceRoot) -and
                -not (Test-AfpDescendantPath -Path $candidate -Parent $Paths.EvidenceRoot)) {
                throw "Evidence target must be within the evidence root: $candidate"
            }
        }
        "Cleanup" {
            if (Test-AfpProtectedTarget -Path $candidate -Paths $Paths) {
                throw "Cleanup target is protected: $candidate"
            }
        }
    }
    return $candidate
}

Export-ModuleMember -Function @(
    "Get-AfpDeploymentPaths",
    "Resolve-AfpSafePath",
    "Assert-AfpMutableTarget",
    "Test-AfpProtectedTarget"
)
