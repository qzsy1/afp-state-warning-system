[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [string]$OutputRoot = (Join-Path $env:TEMP "AFP_Modular_Launcher_v2")
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistRoot = Join-Path $OutputRoot "dist"
$WorkRoot = Join-Path $OutputRoot "work"
$LauncherEntry = Join-Path $ScriptDir "launcher_entry.py"

foreach ($required in @($PythonExecutable, $LauncherEntry)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required launcher build input is missing: $required"
    }
}

$resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
$resolvedOutput = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
if (-not $resolvedOutput.StartsWith($resolvedTemp + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe launcher build directory: $resolvedOutput"
}
if (Test-Path -LiteralPath $resolvedOutput) {
    Remove-Item -LiteralPath $resolvedOutput -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $DistRoot, $WorkRoot | Out-Null

$arguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--noconsole", "--log-level", "WARN",
    "--name", "AFP_Integrated_System_Modular",
    "--collect-all", "torch_geometric",
    "--collect-all", "openpyxl", "--collect-all", "xlrd",
    "--collect-submodules", "mysql.connector",
    "--collect-submodules", "serial",
    "--collect-submodules", "langchain_core",
    "--collect-submodules", "langsmith",
    "--hidden-import", "tkinter", "--hidden-import", "tkinter.filedialog",
    "--hidden-import", "tkinter.messagebox", "--hidden-import", "tkinter.ttk",
    "--hidden-import", "webview", "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "sklearn.ensemble._forest",
    "--hidden-import", "sklearn.ensemble._iforest",
    "--hidden-import", "sklearn.linear_model._logistic",
    "--hidden-import", "sklearn.linear_model._ridge",
    "--hidden-import", "sklearn.svm._classes",
    "--hidden-import", "sklearn.pipeline",
    "--hidden-import", "sklearn.preprocessing._data",
    "--hidden-import", "sklearn.decomposition._pca",
    "--hidden-import", "sklearn.metrics.cluster._expected_mutual_info_fast",
    "--exclude-module", "PyQt5", "--exclude-module", "PySide6",
    "--exclude-module", "IPython", "--exclude-module", "pytest",
    "--exclude-module", "matplotlib", "--exclude-module", "tensorflow",
    "--exclude-module", "tensorboard", "--exclude-module", "keras",
    "--exclude-module", "paddle", "--exclude-module", "cv2",
    "--exclude-module", "kivy", "--exclude-module", "kivy_deps",
    "--exclude-module", "torchaudio", "--exclude-module", "torchvision",
    "--distpath", $DistRoot, "--workpath", $WorkRoot, "--specpath", $WorkRoot,
    $LauncherEntry
)
& $PythonExecutable @arguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller modular launcher build failed: $LASTEXITCODE"
}

$launcherOutput = Join-Path $DistRoot "AFP_Integrated_System_Modular"
$launcherExecutable = Join-Path $launcherOutput "AFP_Integrated_System_Modular.exe"
if (-not (Test-Path -LiteralPath $launcherExecutable -PathType Leaf)) {
    throw "Modular launcher output is missing: $launcherOutput"
}

Write-Output $launcherOutput
