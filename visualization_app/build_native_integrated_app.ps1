param(
    [string]$PythonExecutable = "",
    [string]$TargetDir = "F:\AFP_Integrated_Native_InterfaceMapped\AFP_Integrated_System_SMRF_HID_Fixed_20260823_v1.12.0\AFP_Integrated_System"
)

$ErrorActionPreference = "Stop"
$AppVersion = "1.12.0"
$BuildId = "20260823-schema-contract-fix"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateMonitorDir = Split-Path -Parent $AppDir
$ModelsRoot = Join-Path $AppDir "models"
$RuntimeRoot = Join-Path $AppDir "model_runtime"
$LegacyReplayDir = Join-Path $StateMonitorDir "outputs_tc_hi_soft_consistency_v13_8"
$CausalArtifact = Join-Path $StateMonitorDir "outputs_causal_online_consistency_v13_9\causal_online_consistency_artifact.joblib"
$LegacySource = Get-ChildItem -LiteralPath (Split-Path -Parent $StateMonitorDir) -File -Filter "*.csv" |
    Where-Object { $_.Length -eq 22912911 } |
    Select-Object -First 1 -ExpandProperty FullName

if (-not $PythonExecutable) {
    throw "Pass -PythonExecutable with the Python environment that contains PyInstaller and pywebview."
}

$BuildRoot = Join-Path $env:TEMP "AFPIntegratedNativeBuild_v1_12_0"
$StageRoot = Join-Path $BuildRoot "stage"
$StageData = Join-Path $StageRoot "data"
$StageDemo = Join-Path $StageRoot "new_collection_demo_v11_3"
$DistDir = Join-Path $BuildRoot "dist"
$WorkDir = Join-Path $BuildRoot "work"
$VersionFile = Join-Path $BuildRoot "version_info.txt"

foreach ($Required in @(
    $PythonExecutable, $ModelsRoot, $RuntimeRoot, $LegacyReplayDir,
    $CausalArtifact, $LegacySource,
    (Join-Path $AppDir "static"),
    (Join-Path $AppDir "data\candidate_models"),
    (Join-Path $AppDir "new_collection_demo_v11_3\models\new_collection_hi_artifacts.joblib")
)) {
    if (-not (Test-Path -LiteralPath $Required)) {
        throw "Required build asset missing: $Required"
    }
}

# Recursive cleanup is restricted to this fixed directory below the Windows
# temporary folder. Never remove or overwrite an existing user delivery.
$ResolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
$ResolvedBuild = [IO.Path]::GetFullPath($BuildRoot).TrimEnd('\')
if (-not $ResolvedBuild.StartsWith($ResolvedTemp + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe build directory: $ResolvedBuild"
}
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $TargetDir) {
    throw "Target already exists. Choose a new output directory: $TargetDir"
}
New-Item -ItemType Directory -Force -Path $StageData, $StageDemo, $DistDir, $WorkDir | Out-Null

function Copy-RequiredFile([string]$Source, [string]$DestinationDirectory) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required file missing: $Source"
    }
    New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null
    Copy-Item -LiteralPath $Source -Destination $DestinationDirectory -Force
}

# dashboard_candidate_models.joblib is an obsolete 277 MiB duplicate of the
# candidate_models directory and is deliberately not packaged.
$DashboardFiles = @(
    "dashboard_candidate_catalog.csv", "dashboard_candidate_features.npz",
    "dashboard_candidate_scores.npz", "dashboard_candidate_summary.json",
    "dashboard_manifest.json", "dashboard_sequences.npz",
    "dashboard_window_index.csv", "online_feature_artifacts.joblib"
)
foreach ($Name in $DashboardFiles) {
    Copy-RequiredFile (Join-Path $AppDir "data\$Name") $StageData
}
Copy-Item -LiteralPath (Join-Path $AppDir "data\candidate_models") -Destination $StageData -Recurse
Copy-Item -LiteralPath $LegacyReplayDir -Destination (Join-Path $StageData "legacy_replay") -Recurse
Copy-RequiredFile $CausalArtifact $StageData
Copy-RequiredFile $LegacySource $StageData

# New-scheme data and the fitted 16-sensor health artifact. Old 19-sensor demo
# prediction weights are excluded; current weights live in models/new.
$DemoSource = Join-Path $AppDir "new_collection_demo_v11_3"
foreach ($Name in @(
    "dataset_generation_metadata.json", "manifest.csv", "README.md", "simulator_stream.csv"
)) {
    Copy-RequiredFile (Join-Path $DemoSource $Name) $StageDemo
}
Copy-Item -LiteralPath (Join-Path $DemoSource "raw") -Destination $StageDemo -Recurse
$StageDemoModels = Join-Path $StageDemo "models"
New-Item -ItemType Directory -Force -Path $StageDemoModels | Out-Null
foreach ($Name in @(
    "new_collection_hi_artifacts.joblib", "new_collection_hi_catalog.csv",
    "new_collection_hi_metrics.csv", "new_collection_hi_summary.json",
    "prediction_metrics.json", "test_metrics.json", "training_history.csv"
)) {
    Copy-RequiredFile (Join-Path $DemoSource "models\$Name") $StageDemoModels
}

@"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1,12,0,0), prodvers=(1,12,0,0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'AFP State Warning Research'),
        StringStruct('FileDescription', 'AFP Integrated Acquisition Prediction Training and Warning System'),
        StringStruct('FileVersion', '$AppVersion'),
        StringStruct('InternalName', 'AFP_Integrated_System'),
        StringStruct('OriginalFilename', 'AFP_Integrated_System.exe'),
        StringStruct('ProductName', 'AFP Integrated System'),
        StringStruct('ProductVersion', '$AppVersion')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@ | Set-Content -LiteralPath $VersionFile -Encoding UTF8

$PyInstallerArgs = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--noconsole",
    "--name", "AFP_Integrated_System", "--version-file", $VersionFile,
    "--paths", $RuntimeRoot,
    "--add-data", "$(Join-Path $AppDir 'static');static",
    "--add-data", "$StageData;data",
    "--add-data", "$StageDemo;new_collection_demo_v11_3",
    "--add-data", "$RuntimeRoot;model_runtime",
    # I-ModernTCN triggers TorchScript source inspection inside PyG.  The
    # package source files therefore have to be present, not only bytecode.
    "--collect-all", "torch_geometric",
    "--collect-submodules", "mysql.connector",
    "--collect-submodules", "serial",
    "--collect-submodules", "langchain_core",
    "--collect-submodules", "langsmith",
    "--hidden-import", "sklearn.ensemble._forest",
    "--hidden-import", "sklearn.ensemble._iforest",
    "--hidden-import", "sklearn.linear_model._logistic",
    "--hidden-import", "sklearn.linear_model._ridge",
    "--hidden-import", "sklearn.svm._classes",
    "--hidden-import", "sklearn.pipeline",
    "--hidden-import", "sklearn.preprocessing._data",
    "--hidden-import", "sklearn.decomposition._pca",
    "--hidden-import", "sklearn.metrics.cluster._expected_mutual_info_fast",
    "--hidden-import", "app", "--hidden-import", "acquisition", "--hidden-import", "interface_agent",
    "--hidden-import", "mysql_storage", "--hidden-import", "native_integrated_app",
    "--hidden-import", "native_frontend_launcher", "--hidden-import", "webview",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "online_inference", "--hidden-import", "atavn",
    "--hidden-import", "online_health_features", "--hidden-import", "causal_online_runtime",
    "--hidden-import", "runtime_scaler", "--hidden-import", "new_collection_health",
    "--hidden-import", "runtime_health_primitives", "--hidden-import", "web_training",
    "--hidden-import", "web_training_pipeline", "--hidden-import", "training_data",
    "--hidden-import", "shijie.model_mine.I_modernTCN_GAT_abalation",
    "--hidden-import", "models.TCN", "--hidden-import", "models.Transformer",
    "--hidden-import", "models.Informer", "--hidden-import", "models.DeepVAR",
    "--hidden-import", "models.DLinear", "--hidden-import", "models.NLinear",
    "--hidden-import", "models.Linear", "--hidden-import", "models.MLP",
    "--hidden-import", "models.FNN",
    "--exclude-module", "PyQt5", "--exclude-module", "PySide6",
    "--exclude-module", "IPython", "--exclude-module", "pytest",
    "--exclude-module", "matplotlib", "--exclude-module", "tensorflow",
    "--exclude-module", "tensorboard", "--exclude-module", "keras",
    "--exclude-module", "paddle", "--exclude-module", "cv2",
    "--exclude-module", "torchaudio", "--exclude-module", "torchvision",
    "--distpath", $DistDir, "--workpath", $WorkDir, "--specpath", $WorkDir,
    (Join-Path $AppDir "native_frontend_launcher.py")
)
& $PythonExecutable @PyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }

$Built = Join-Path $DistDir "AFP_Integrated_System"
$BuiltExe = Join-Path $Built "AFP_Integrated_System.exe"
if (-not (Test-Path -LiteralPath $BuiltExe -PathType Leaf)) {
    throw "Executable missing after build: $BuiltExe"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetDir) | Out-Null
Copy-Item -LiteralPath $Built -Destination $TargetDir -Recurse

# One external weight tree supports 11 thesis algorithms in both schemas,
# without duplicating weights in _internal.
Copy-Item -LiteralPath $ModelsRoot -Destination (Join-Path $TargetDir "models") -Recurse

$SourceTarget = Join-Path $TargetDir "source\visualization_app"
New-Item -ItemType Directory -Force -Path $SourceTarget | Out-Null
$SourceFiles = @(
    "native_frontend_launcher.py", "native_integrated_app.py", "app.py",
    "acquisition.py", "interface_agent.py", "mysql_storage.py", "online_inference.py", "atavn.py",
    "online_health_features.py", "causal_online_runtime.py", "runtime_scaler.py",
    "new_collection_health.py", "fit_new_collection_health.py", "runtime_health_primitives.py", "web_training.py",
    "web_training_pipeline.py", "training_data.py", "build_native_integrated_app.ps1",
    "test_app.py", "test_mysql_identity.py", "test_new_collection_health.py",
    "test_native_integrated_app.py", "README.md"
)
foreach ($Name in $SourceFiles) {
    $Source = Join-Path $AppDir $Name
    if (Test-Path -LiteralPath $Source -PathType Leaf) {
        Copy-Item -LiteralPath $Source -Destination $SourceTarget
    }
}
Copy-Item -LiteralPath (Join-Path $AppDir "static") -Destination $SourceTarget -Recurse
Copy-Item -LiteralPath $RuntimeRoot -Destination $SourceTarget -Recurse

$ModelManifest = [ordered]@{
    application_version = $AppVersion
    build_id = $BuildId
    schemas = @{
        legacy = @{ sensor_count = 12; model_input_count = 15 }
        new = @{ sensor_count = 16; model_input_count = 20 }
    }
    algorithms = @(
        "i_T_G", "TCN", "Transformer", "Informer", "DeepVAR", "DLinear",
        "NLinear", "Linear", "MLP", "FNN_2024", "FNN_2025_Base"
    )
    model_count = 22
    weights_location = "models/<schema>/<algorithm>/checkpoint.pth"
    health_schema = "new_collection_hi_v3_16s4p"
}
$ModelManifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $TargetDir "MODEL_MANIFEST.json") -Encoding UTF8
@{
    version = $AppVersion; build_id = $BuildId
    built_at = (Get-Date).ToString("o"); python = $PythonExecutable
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $TargetDir "VERSION.json") -Encoding UTF8

@(
    "AFP Integrated Acquisition Prediction Training and Warning System v$AppVersion",
    "=========================================================================",
    "Start: double-click AFP_Integrated_System.exe", "",
    "This is a native desktop window. Its internal loopback service uses an ephemeral port; no external browser or fixed URL is required.",
    "Keep AFP_Integrated_System.exe, _internal, and models in the same directory.",
    "models contains 11 thesis prediction algorithms for both legacy and new schemas (22 checkpoints).",
    "The new schema strictly uses 16 measured sensor channels and 4 process parameters. Removed channels are never filled with virtual values.",
    "", "See the verification directory for executable test reports.",
    "This build is unsigned because no Windows code-signing certificate was supplied."
) | Set-Content -LiteralPath (Join-Path $TargetDir "README.txt") -Encoding UTF8

$Hashes = Get-ChildItem -LiteralPath $TargetDir -Recurse -File | Where-Object {
    $_.FullName -notlike "*\SHA256SUMS.txt"
} | Get-FileHash -Algorithm SHA256
$Hashes | ForEach-Object {
    $Relative = $_.Path.Substring($TargetDir.Length).TrimStart('\')
    "$($_.Hash) *$Relative"
} | Set-Content -LiteralPath (Join-Path $TargetDir "SHA256SUMS.txt") -Encoding ASCII

Write-Host "Built AFP Integrated System v$AppVersion"
Write-Host "Output: $TargetDir"
