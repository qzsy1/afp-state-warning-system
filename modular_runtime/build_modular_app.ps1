param(
    [string]$PythonExecutable = "",
    [string]$ReferenceRelease = "F:\AFP_Integrated_Native_InterfaceMapped\AFP_Integrated_System_SMRF_HID_Restored_20260824_v1.12.1\AFP_Integrated_System",
    [string]$TargetDir = "F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.1",
    [string]$ApplicationVersion = "2.0.2",
    [switch]$SkipExecutableBuild,
    [switch]$AllowExistingTarget,
    [string]$ExistingExecutable = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$LegacySource = Join-Path $RepoRoot "visualization_app"
$BuildRoot = Join-Path $env:TEMP "AFP_Modular_Launcher_v2"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "work"
$referenceInternal = Join-Path $ReferenceRelease "_internal"
$referenceLegacy = Join-Path $ReferenceRelease "app\legacy"
$referenceData = Join-Path $referenceInternal "data"
if (-not (Test-Path -LiteralPath $referenceData) -and (Test-Path -LiteralPath (Join-Path $referenceLegacy "data"))) {
    $referenceData = Join-Path $referenceLegacy "data"
}

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
    $referenceData,
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
if ((Test-Path -LiteralPath $TargetDir) -and -not $AllowExistingTarget) {
    throw "Target already exists; choose a new directory: $TargetDir"
}
New-Item -ItemType Directory -Force -Path $DistRoot, $WorkRoot | Out-Null

if (-not $SkipExecutableBuild -and -not $ExistingExecutable) {
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
        "--hidden-import", "interface_agent",
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
        "--exclude-module", "clr_loader", "--exclude-module", "pythonnet",
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
if ($ExistingExecutable) {
    if (-not (Test-Path -LiteralPath $ExistingExecutable)) {
        throw "Existing executable is missing: $ExistingExecutable"
    }
    $built = Join-Path $BuildRoot "prebuilt_launcher"
    New-Item -ItemType Directory -Force -Path $built | Out-Null
    Copy-Item -LiteralPath $ExistingExecutable -Destination (Join-Path $built "AFP_Integrated_System_Modular.exe") -Force
} elseif ($SkipExecutableBuild) {
    $built = Join-Path $ScriptDir "prebuilt_launcher"
}
if (-not (Test-Path -LiteralPath (Join-Path $built "AFP_Integrated_System_Modular.exe"))) {
    throw "Modular launcher output is missing: $built"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetDir), $TargetDir | Out-Null
if ($AllowExistingTarget) {
    Copy-Item -Path (Join-Path $built "*") -Destination $TargetDir -Recurse -Force
} else {
    Copy-Item -LiteralPath $built -Destination $TargetDir -Recurse
}

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
New-Item -ItemType Directory -Force -Path (Join-Path $appTarget "core"), (Join-Path $appTarget "modules") | Out-Null
Copy-Item -Path (Join-Path $ScriptDir "app\core\*") -Destination (Join-Path $appTarget "core") -Recurse -Force
Copy-Item -Path (Join-Path $ScriptDir "app\modules\*") -Destination (Join-Path $appTarget "modules") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $ScriptDir "config\runtime.delivery.json") -Destination (Join-Path $configTarget "runtime.json") -Force
$documentation = Get-ChildItem -LiteralPath (Join-Path $RepoRoot "docs") -Filter "*.md" -File
if (-not $documentation) {
    throw "Modular documentation directory does not contain Markdown files."
}
$documentation | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $docsTarget -Force
}

$legacyFiles = @(
    "app.py", "interface_agent.py", "agentic_diagnosis.py", "diagnostic_tools.py", "acquisition.py", "smrf_hid.py", "mysql_storage.py",
    "online_inference.py", "atavn.py", "online_health_features.py",
    "causal_online_runtime.py", "runtime_scaler.py", "new_collection_health.py",
    "runtime_health_primitives.py", "web_training.py", "web_training_pipeline.py",
    "training_data.py", "training_center.py", "training_center_cli.py",
    "native_integrated_app.py", "fit_new_collection_health.py", "websocket_live.py",
    "helper_relay.py", "edge_capture.py", "local_capture_agent.py", "local_capture_helper_entry.py",
    "remote_mysql_setup.py", "server_target_mysql.py", "server_capture_journal.py", "generate_pressure_simulation.py",
    "web_auth.py", "web_access.py", "public_status.py", "guest_simulation.py", "control_lease.py", "json_safety.py"
)
foreach ($name in $legacyFiles) {
    $source = Join-Path $LegacySource $name
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Legacy compatibility source missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination $legacyTarget -Force
}
foreach ($name in @("web_auth.py", "web_access.py", "public_status.py", "guest_simulation.py", "control_lease.py", "json_safety.py")) {
    $copied = Join-Path $legacyTarget $name
    if (-not (Test-Path -LiteralPath $copied)) {
        throw "Public-web compatibility source was not copied: $copied"
    }
}
Get-ChildItem -LiteralPath (Join-Path $LegacySource "static") -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $uiTarget -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir "models"), (Join-Path $legacyTarget "data") | Out-Null
Copy-Item -Path (Join-Path $ReferenceRelease "models\*") -Destination (Join-Path $TargetDir "models") -Recurse -Force
Copy-Item -Path (Join-Path $referenceData "*") -Destination (Join-Path $legacyTarget "data") -Recurse -Force
if (Test-Path -LiteralPath (Join-Path $referenceData "new_collection_demo_v11_3")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "new_collection_demo_v11_3") | Out-Null
    Copy-Item -Path (Join-Path $referenceData "new_collection_demo_v11_3\*") -Destination (Join-Path $legacyTarget "new_collection_demo_v11_3") -Recurse -Force
} elseif (Test-Path -LiteralPath (Join-Path $referenceLegacy "new_collection_demo_v11_3")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "new_collection_demo_v11_3") | Out-Null
    Copy-Item -Path (Join-Path $referenceLegacy "new_collection_demo_v11_3\*") -Destination (Join-Path $legacyTarget "new_collection_demo_v11_3") -Recurse -Force
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "new_collection_demo_v11_3") | Out-Null
    Copy-Item -Path (Join-Path $LegacySource "new_collection_demo_v11_3\*") -Destination (Join-Path $legacyTarget "new_collection_demo_v11_3") -Recurse -Force
}
if (Test-Path -LiteralPath (Join-Path $referenceData "model_runtime")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "model_runtime") | Out-Null
    Copy-Item -Path (Join-Path $referenceData "model_runtime\*") -Destination (Join-Path $legacyTarget "model_runtime") -Recurse -Force
} elseif (Test-Path -LiteralPath (Join-Path $referenceLegacy "model_runtime")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "model_runtime") | Out-Null
    Copy-Item -Path (Join-Path $referenceLegacy "model_runtime\*") -Destination (Join-Path $legacyTarget "model_runtime") -Recurse -Force
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "model_runtime") | Out-Null
    Copy-Item -Path (Join-Path $LegacySource "model_runtime\*") -Destination (Join-Path $legacyTarget "model_runtime") -Recurse -Force
}
Get-ChildItem -LiteralPath (Join-Path $LegacySource "hardware_dlls") -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $nativeTarget -Force
}

foreach ($directory in @("logs", "runtime", "rollback", "updates", "verification")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir $directory) | Out-Null
}

# Delivery trees must never contain runtime credentials or tunnel secrets.
$mutableDirectories = @("logs", "runtime", "rollback", "updates", "verification")
$forbiddenNames = Get-ChildItem -LiteralPath $TargetDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($TargetDir.Length).TrimStart('\\')
    $topDirectory = ($relative -split '[\\/]')[0]
    $topDirectory -notin $mutableDirectories -and (
        $_.Name -in @("public_web_security.sqlite3", "cert.pem") -or
        $_.Name -like "*.cfargotunnel.com.json"
    )
}
if ($forbiddenNames) {
    throw "Delivery contains a forbidden public-web secret file: $($forbiddenNames[0].FullName)"
}
$textExtensions = @(".py", ".ps1", ".json", ".txt", ".md", ".html", ".js", ".css", ".toml", ".yaml", ".yml")
foreach ($file in (Get-ChildItem -LiteralPath $TargetDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($TargetDir.Length).TrimStart('\\')
    $topDirectory = ($relative -split '[\\/]')[0]
    $topDirectory -notin $mutableDirectories -and $textExtensions -contains $_.Extension.ToLowerInvariant()
})) {
    $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop
    if ($text -match 'sk-[A-Za-z0-9_-]{20,}') {
        throw "Delivery text contains a SiliconFlow-style API key: $($file.FullName)"
    }
}

@{
    application_version = $ApplicationVersion
    launcher_version = $ApplicationVersion
    module_api_version = "2.0"
    baseline = "v1.12.1-r3"
    built_at = (Get-Date).ToString("o")
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $TargetDir "VERSION.json") -Encoding UTF8

@(
    "AFP Integrated System Modular v$ApplicationVersion",
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
