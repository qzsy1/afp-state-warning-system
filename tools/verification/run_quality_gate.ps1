[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("quick", "full", "release")]
    [string]$Profile,

    [ValidateSet("local", "ci")]
    [string]$Environment = "local",

    [string]$Matrix = "verification/regression-matrix.json",
    [string]$ExeRules = "verification/exe-rebuild-rules.json",
    [string]$BaseRef,
    [string[]]$ChangedFile = @(),
    [string]$BaselineExe,
    [string]$BaselineExeSha256,
    [string]$ReportDir = "verification/results"
)

$ErrorActionPreference = "Stop"
$pathSources = @(
    $env:Path,
    [Environment]::GetEnvironmentVariable("Path", "Machine"),
    [Environment]::GetEnvironmentVariable("Path", "User")
)
$env:Path = ($pathSources | Where-Object { $_ } | ForEach-Object {
    [Environment]::ExpandEnvironmentVariables($_)
}) -join ";"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$scriptPath = Join-Path $PSScriptRoot "quality_gate.py"

function Test-PythonCandidate {
    param([hashtable]$Candidate)
    $command = $Candidate.Command
    if ($Candidate.RequiresFile -and -not (Test-Path -LiteralPath $command -PathType Leaf)) {
        return $false
    }
    if (-not $Candidate.RequiresFile -and -not (Get-Command $command -ErrorAction SilentlyContinue)) {
        return $false
    }
    $prefix = @($Candidate.Prefix)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $command @prefix -c "import sys; raise SystemExit(0)" *> $null
        $candidateExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return $candidateExitCode -eq 0
}

$candidates = @()
if ($env:AFP_PYTHON) {
    $candidates += @{ Command = $env:AFP_PYTHON; Prefix = @(); RequiresFile = $true; Name = "AFP_PYTHON" }
}
$candidates += @{ Command = (Join-Path $repoRoot ".venv\Scripts\python.exe"); Prefix = @(); RequiresFile = $true; Name = ".venv" }
$candidates += @{ Command = "py"; Prefix = @("-3.11"); RequiresFile = $false; Name = "py -3.11" }
$candidates += @{ Command = "py"; Prefix = @("-3"); RequiresFile = $false; Name = "py -3" }
$candidates += @{ Command = "python"; Prefix = @(); RequiresFile = $false; Name = "python" }

$selected = $null
foreach ($candidate in $candidates) {
    if (Test-PythonCandidate -Candidate $candidate) {
        $selected = $candidate
        break
    }
}
if (-not $selected) {
    Write-Error "No usable Python interpreter was found. Set AFP_PYTHON or install Python 3.11+."
    exit 2
}

$arguments = @(
    $scriptPath,
    "--profile", $Profile,
    "--environment", $Environment,
    "--matrix", $Matrix,
    "--exe-rules", $ExeRules,
    "--report-dir", $ReportDir
)
if ($BaseRef) {
    $arguments += @("--base-ref", $BaseRef)
}
foreach ($path in $ChangedFile) {
    $arguments += @("--changed-file", $path)
}
if ($BaselineExe) {
    $arguments += @("--baseline-exe", $BaselineExe)
}
if ($BaselineExeSha256) {
    $arguments += @("--baseline-exe-sha256", $BaselineExeSha256)
}

Push-Location $repoRoot
try {
    $command = $selected.Command
    $prefix = @($selected.Prefix)
    & $command @prefix @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode
