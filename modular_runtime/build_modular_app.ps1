[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$ReferenceRelease = "D:\AFP_Runtime\AFP_Integrated_System_Modular",
    [string]$TargetDir = "",
    [string]$ApplicationVersion = "2.0.2",
    [switch]$SkipExecutableBuild,
    [switch]$AllowExistingTarget,
    [string]$ExistingExecutable = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildLauncher = Join-Path $ScriptDir "build_launcher.ps1"
$AssembleDelivery = Join-Path $ScriptDir "assemble_modular_delivery.ps1"
if (-not $TargetDir) {
    $TargetDir = Join-Path "D:\AFP_Runtime\.afp-staging" ("manual-build-" + [guid]::NewGuid().ToString("N"))
}

foreach ($script in @($BuildLauncher, $AssembleDelivery)) {
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        throw "Required modular build script is missing: $script"
    }
}

$assemblyArguments = @{
    ReferenceRelease = $ReferenceRelease
    TargetDir = $TargetDir
    ApplicationVersion = $ApplicationVersion
    AllowExistingTarget = [bool]$AllowExistingTarget
}

if ($ExistingExecutable) {
    $assemblyArguments.ExistingExecutable = $ExistingExecutable
} elseif ($SkipExecutableBuild) {
    $assemblyArguments.LauncherSourceDir = Join-Path $ScriptDir "prebuilt_launcher"
} else {
    if (-not $PythonExecutable) {
        $referenceVersion = Join-Path $ReferenceRelease "VERSION.json"
        if (Test-Path -LiteralPath $referenceVersion -PathType Leaf) {
            $PythonExecutable = (Get-Content -LiteralPath $referenceVersion -Raw -Encoding UTF8 | ConvertFrom-Json).python
        }
    }
    if (-not $PythonExecutable) {
        throw "PythonExecutable is required when building the launcher."
    }
    $launcherOutputRoot = Join-Path "D:\AFP_Runtime\.afp-staging" "AFP_Modular_Launcher_v2"
    & $BuildLauncher -PythonExecutable $PythonExecutable -OutputRoot $launcherOutputRoot
    $assemblyArguments.LauncherSourceDir = Join-Path $launcherOutputRoot "dist\AFP_Integrated_System_Modular"
}

& $AssembleDelivery @assemblyArguments
