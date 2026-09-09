param(
    [string]$PythonExecutable = "",
    [string]$ReferenceRelease = "F:\AFP_Integrated_Native_InterfaceMapped\AFP_Integrated_System_SMRF_HID_Restored_20260824_v1.12.1\AFP_Integrated_System",
    [string]$TargetDir = "F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.1",
    [switch]$SkipExecutableBuild
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$LegacySource = Join-Path $RepoRoot "visualization_app"
$BuildRoot = Join-Path $env:TEMP "AFP_Modular_Launcher_v2"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "work"

if (-not $PythonExecutable) {
    $referenceVersion = Join-Path $ReferenceRelease "VERSION.json"
    if (Test-Path -LiteralPath $referenceVersion) {
        $PythonExecutable = (Get-Content -LiteralPath $referenceVersion -Raw | ConvertFrom-Json).python
    }
}

foreach ($required in @(
    $PythonExecutable,
    $ReferenceRelease,
    (Join-Path $ScriptDir "launcher_entry.py"),
    (Join-Path $ScriptDir "app\bootstrap.py"),
    (Join-Path $LegacySource "app.py"),
    (Join-Path $ReferenceRelease "_internal\data"),
    (Join-Path $ReferenceRelease "models")
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required modular build input is missing: $required"
    }
}

$resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
$resolvedBuild = [IO.Path]::GetFullPath($BuildRoot).TrimEnd('\')
if (-not $resolvedBuild.StartsWith($resolvedTemp + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe build directory: $resolvedBuild"
}
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $TargetDir) {
    throw "Target already exists; choose a new directory: $TargetDir"
}
New-Item -ItemType Directory -Force -Path $DistRoot, $WorkRoot | Out-Null

if (-not $SkipExecutableBuild) {
    $arguments = @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--noconsole",
        "--name", "AFP_Integrated_System_Modular",
        "--collect-all", "torch_geometric",
        "--collect-all", "openpyxl", "--collect-all", "xlrd",
        "--collect-submodules", "mysql.connector",
        "--collect-submodules", "serial",
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
        "--exclude-module", "torchaudio", "--exclude-module", "torchvision",
        "--distpath", $DistRoot, "--workpath", $WorkRoot, "--specpath", $WorkRoot,
        (Join-Path $ScriptDir "launcher_entry.py")
    )
    & $PythonExecutable @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller modular launcher build failed: $LASTEXITCODE"
    }
}

$built = Join-Path $DistRoot "AFP_Integrated_System_Modular"
if ($SkipExecutableBuild) {
    $built = Join-Path $ScriptDir "prebuilt_launcher"
}
if (-not (Test-Path -LiteralPath (Join-Path $built "AFP_Integrated_System_Modular.exe"))) {
    throw "Modular launcher output is missing: $built"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetDir) | Out-Null
Copy-Item -LiteralPath $built -Destination $TargetDir -Recurse

$appTarget = Join-Path $TargetDir "app"
$legacyTarget = Join-Path $appTarget "legacy"
$uiTarget = Join-Path $appTarget "ui"
$configTarget = Join-Path $TargetDir "config"
$nativeTarget = Join-Path $TargetDir "native_dll"
$docsTarget = Join-Path $TargetDir "docs"
foreach ($path in @($appTarget, $legacyTarget, $uiTarget, $configTarget, $nativeTarget, $docsTarget)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

Copy-Item -LiteralPath (Join-Path $ScriptDir "app\bootstrap.py") -Destination $appTarget -Force
Copy-Item -LiteralPath (Join-Path $ScriptDir "app\core") -Destination $appTarget -Recurse
Copy-Item -LiteralPath (Join-Path $ScriptDir "app\modules") -Destination $appTarget -Recurse
Copy-Item -LiteralPath (Join-Path $ScriptDir "config\runtime.delivery.json") -Destination (Join-Path $configTarget "runtime.json") -Force
$documentation = Get-ChildItem -LiteralPath (Join-Path $RepoRoot "docs") -Filter "*.md" -File
if (-not $documentation) {
    throw "Modular documentation directory does not contain Markdown files."
}
$documentation | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $docsTarget -Force
}

$legacyFiles = @(
    "app.py", "acquisition.py", "smrf_hid.py", "mysql_storage.py",
    "online_inference.py", "atavn.py", "online_health_features.py",
    "causal_online_runtime.py", "runtime_scaler.py", "new_collection_health.py",
    "runtime_health_primitives.py", "web_training.py", "web_training_pipeline.py",
    "training_data.py", "training_center.py", "training_center_cli.py",
    "native_integrated_app.py", "fit_new_collection_health.py",
    "remote_mysql_setup.py"
)
foreach ($name in $legacyFiles) {
    $source = Join-Path $LegacySource $name
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Legacy compatibility source missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination $legacyTarget -Force
}
Get-ChildItem -LiteralPath (Join-Path $LegacySource "static") -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $uiTarget -Recurse -Force
}

$referenceInternal = Join-Path $ReferenceRelease "_internal"
Copy-Item -LiteralPath (Join-Path $ReferenceRelease "models") -Destination $TargetDir -Recurse
Copy-Item -LiteralPath (Join-Path $referenceInternal "data") -Destination $legacyTarget -Recurse
if (Test-Path -LiteralPath (Join-Path $referenceInternal "new_collection_demo_v11_3")) {
    Copy-Item -LiteralPath (Join-Path $referenceInternal "new_collection_demo_v11_3") -Destination $legacyTarget -Recurse
}
if (Test-Path -LiteralPath (Join-Path $referenceInternal "model_runtime")) {
    Copy-Item -LiteralPath (Join-Path $referenceInternal "model_runtime") -Destination $legacyTarget -Recurse
} else {
    Copy-Item -LiteralPath (Join-Path $LegacySource "model_runtime") -Destination $legacyTarget -Recurse
}
Get-ChildItem -LiteralPath (Join-Path $LegacySource "hardware_dlls") -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $nativeTarget -Force
}

foreach ($directory in @("logs", "runtime", "rollback", "updates", "verification")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir $directory) | Out-Null
}

@{
    application_version = "2.0.1"
    launcher_version = "2.0.1"
    module_api_version = "2.0"
    baseline = "v1.12.1-r3"
    built_at = (Get-Date).ToString("o")
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $TargetDir "VERSION.json") -Encoding UTF8

@(
    "AFP Integrated System Modular v2.0.1",
    "====================================",
    "Start: AFP_Integrated_System_Modular.exe",
    "Application logic is stored in app/modules and app/legacy and can be updated without rebuilding the EXE.",
    "UI files are in app/ui; configuration is in config/runtime.json; prediction weights are in models.",
    "Keep the complete directory together.  Another PC does not need Python, PyTorch or Git.",
    "Use --module-status or --self-test for diagnostics; use --verify-files for SHA-256 integrity verification.",
    "Use --mysql-smoke and --spreadsheet-smoke to verify database and Excel runtime dependencies.",
    "Use verified patch ZIP files for module updates. Mutable logs, runtime data, update packages, verification outputs and Python caches are not in SHA256SUMS.txt."
) | Set-Content -LiteralPath (Join-Path $TargetDir "README.txt") -Encoding UTF8

$mutableDirectories = @("logs", "runtime", "rollback", "updates", "verification")
$hashes = Get-ChildItem -LiteralPath $TargetDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($TargetDir.Length).TrimStart('\')
    $topDirectory = ($relative -split '[\\/]')[0]
    $segments = $relative -split '[\\/]'
    $_.Name -ne "SHA256SUMS.txt" -and
        $topDirectory -notin $mutableDirectories -and
        $_.Extension -ne ".pyc" -and
        "__pycache__" -notin $segments
} | Get-FileHash -Algorithm SHA256
$hashes | ForEach-Object {
    $relative = $_.Path.Substring($TargetDir.Length).TrimStart('\')
    "$($_.Hash) *$relative"
} | Set-Content -LiteralPath (Join-Path $TargetDir "SHA256SUMS.txt") -Encoding UTF8

Write-Host "Modular delivery created: $TargetDir"
