[CmdletBinding()]
param(
    [ValidateSet("Inspect", "StartTask", "PrepareMerge", "VerifyMerge", "UpdateMain")]
    [string]$Action = "Inspect",

    [string]$TaskSlug
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Get-GitText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return ($output | Out-String).Trim()
}

function Test-GitSuccess {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & git @Arguments *> $null
    return $LASTEXITCODE -eq 0
}

function Assert-OrdinaryCheckout {
    $gitDir = Get-GitText @("rev-parse", "--path-format=absolute", "--git-dir")
    $commonDir = Get-GitText @("rev-parse", "--path-format=absolute", "--git-common-dir")
    $gitPath = [System.IO.Path]::GetFullPath($gitDir).TrimEnd('\', '/')
    $commonPath = [System.IO.Path]::GetFullPath($commonDir).TrimEnd('\', '/')
    if (-not [string]::Equals($gitPath, $commonPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Worktrees are prohibited. Git directory is '$gitPath' and common directory is '$commonPath'."
    }
}

function Get-MachineIdentity {
    $identity = & git config --local --get codex.machine-id
    $configExitCode = $LASTEXITCODE
    if ($configExitCode -eq 1) {
        $skillPath = Join-Path $repositoryRoot ".agents/skills/local-branch-workflow/SKILL.md"
        $identity = if (Test-Path -LiteralPath $skillPath -PathType Leaf) { "PC-B" } else { "PC-A" }
        Invoke-Git @("config", "--local", "codex.machine-id", $identity)
        $identity = Get-GitText @("config", "--local", "--get", "codex.machine-id")
    } elseif ($configExitCode -ne 0) {
        throw "Cannot read clone-local codex.machine-id (exit $configExitCode)."
    }
    if ([string]::IsNullOrWhiteSpace($identity) -or $identity -notmatch '^[A-Za-z0-9][A-Za-z0-9-]*$') {
        throw "codex.machine-id '$identity' cannot be used in a branch name."
    }
    return $identity
}

function Get-RemoteDefaultBranch {
    $remoteHead = Get-GitText @("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if ($remoteHead -notmatch '^origin/(.+)$') {
        throw "Cannot determine the default branch from origin/HEAD."
    }
    return $Matches[1]
}

function Get-CurrentBranch {
    $branch = Get-GitText @("branch", "--show-current")
    if ([string]::IsNullOrWhiteSpace($branch)) {
        throw "Detached HEAD is not supported by this workflow."
    }
    return $branch
}

function Assert-CleanWorktree {
    $status = Get-GitText @("status", "--porcelain=v1", "--untracked-files=all")
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw "The worktree contains tracked or untracked changes. Preserve and resolve them before changing branches or synchronizing."
    }
}

function Assert-OwnedTaskBranch {
    param(
        [Parameter(Mandatory = $true)][string]$Branch,
        [Parameter(Mandatory = $true)][string]$MachineIdentity,
        [Parameter(Mandatory = $true)][string]$DefaultBranch
    )

    if ($Branch -eq $DefaultBranch) {
        throw "This action is prohibited on '$DefaultBranch'."
    }
    $ownedPrefix = "codex/$($MachineIdentity.ToLowerInvariant())/"
    if (-not $Branch.StartsWith($ownedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Branch '$Branch' is not owned by machine '$MachineIdentity'. Expected prefix '$ownedPrefix'."
    }
}

function Assert-NoUnmergedPaths {
    $paths = Get-GitText @("diff", "--name-only", "--diff-filter=U")
    if (-not [string]::IsNullOrWhiteSpace($paths)) {
        throw "The branch contains unresolved merge conflicts."
    }
}

$repositoryRoot = Get-GitText @("rev-parse", "--show-toplevel")
Set-Location -LiteralPath $repositoryRoot
Assert-OrdinaryCheckout
$machineIdentity = Get-MachineIdentity
$machineSlug = $machineIdentity.ToLowerInvariant()
$defaultBranch = Get-RemoteDefaultBranch
$currentBranch = Get-CurrentBranch

switch ($Action) {
    "Inspect" {
        Write-Output "repository=$repositoryRoot"
        Write-Output "machine-id=$machineIdentity"
        Write-Output "default-branch=$defaultBranch"
        Write-Output "current-branch=$currentBranch"
        Invoke-Git @("status", "--short", "--branch")
    }

    "StartTask" {
        if ($currentBranch.StartsWith("codex/$machineSlug/", [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Output "Continuing owned task branch '$currentBranch'."
            Invoke-Git @("fetch", "--prune", "origin")
            break
        }
        if ($currentBranch -ne $defaultBranch) {
            throw "Cannot start a task from '$currentBranch'. Switch safely to '$defaultBranch' first."
        }
        Assert-CleanWorktree
        if ([string]::IsNullOrWhiteSpace($TaskSlug)) {
            throw "-TaskSlug is required for StartTask."
        }
        $normalizedSlug = $TaskSlug.ToLowerInvariant() -replace '[^a-z0-9]+', '-'
        $normalizedSlug = $normalizedSlug.Trim('-')
        if ([string]::IsNullOrWhiteSpace($normalizedSlug)) {
            throw "TaskSlug must contain at least one letter or digit."
        }
        $taskBranch = "codex/$machineSlug/$normalizedSlug"
        Invoke-Git @("fetch", "--prune", "origin")
        Invoke-Git @("merge", "--ff-only", "origin/$defaultBranch")
        if (Test-GitSuccess @("show-ref", "--verify", "--quiet", "refs/heads/$taskBranch")) {
            throw "Local branch '$taskBranch' already exists."
        }
        if (Test-GitSuccess @("show-ref", "--verify", "--quiet", "refs/remotes/origin/$taskBranch")) {
            throw "Remote branch 'origin/$taskBranch' already exists. Do not reuse another computer's work."
        }
        Invoke-Git @("switch", "--no-track", "-c", $taskBranch, "origin/$defaultBranch")
    }

    "PrepareMerge" {
        Assert-OwnedTaskBranch -Branch $currentBranch -MachineIdentity $machineIdentity -DefaultBranch $defaultBranch
        Assert-NoUnmergedPaths
        Assert-CleanWorktree
        Invoke-Git @("fetch", "--prune", "origin")
        if (-not (Test-GitSuccess @("merge-base", "--is-ancestor", "origin/$defaultBranch", "HEAD"))) {
            Invoke-Git @("merge", "--no-edit", "origin/$defaultBranch")
        }
        Assert-NoUnmergedPaths
        Invoke-Git @("diff", "--check")
        Invoke-Git @("status", "--short", "--branch")
        Write-Output "Run the relevant repository tests, then use -Action VerifyMerge."
    }

    "VerifyMerge" {
        Assert-OwnedTaskBranch -Branch $currentBranch -MachineIdentity $machineIdentity -DefaultBranch $defaultBranch
        Assert-NoUnmergedPaths
        Assert-CleanWorktree
        Invoke-Git @("fetch", "origin")
        if (-not (Test-GitSuccess @("merge-base", "--is-ancestor", "origin/$defaultBranch", "HEAD"))) {
            throw "origin/$defaultBranch advanced after testing. Run PrepareMerge, repeat the relevant tests, and verify again."
        }
        Invoke-Git @("diff", "--check")
        Invoke-Git @("status", "--short", "--branch")
        Write-Output "The tested task branch contains the current origin/$defaultBranch."
    }

    "UpdateMain" {
        if ($currentBranch -ne $defaultBranch) {
            throw "UpdateMain requires branch '$defaultBranch'; current branch is '$currentBranch'."
        }
        Assert-CleanWorktree
        Invoke-Git @("fetch", "--prune", "origin")
        Invoke-Git @("merge", "--ff-only", "origin/$defaultBranch")
        Invoke-Git @("status", "--short", "--branch")
    }
}
