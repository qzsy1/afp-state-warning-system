param(
    [string]$PythonExecutable = "F:\基于数字孪生的故障诊断\自动铺丝\Program\afp-state-warning-system-1.9.0\visualization_app\build_venv\Scripts\python.exe",
    [string]$TargetDir = "F:\基于数字孪生的故障诊断\自动铺丝\Program\AFP_Integrated_Native_System"
)
$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateMonitorDir = Split-Path -Parent $AppDir
$ProjectRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $StateMonitorDir))
$LegacyCheckpoint = Join-Path $ProjectRoot "checkpoints\health_i_T_G_MyCustom_ftM_sl24_ll24_pl24_dm128_nh8_el2_dl1_df2048_fc1_ebtimeF_dtTrue_health_v9_conditional_normal_no_param_score_lr0.001_bs128\checkpoint.pth"
$LegacyReplayDir = Join-Path $StateMonitorDir "outputs_tc_hi_soft_consistency_v13_8"
$CausalArtifact = Join-Path $StateMonitorDir "outputs_causal_online_consistency_v13_9\causal_online_consistency_artifact.joblib"
$ModelRuntimeRoot = "F:\program\XJUsorceopen"
$TrainingCore = Join-Path (Split-Path -Parent $StateMonitorDir) "final_training_packages\new_data_full_pipeline"
$LegacySource = Get-ChildItem -LiteralPath (Split-Path -Parent $StateMonitorDir) -File -Filter "*.csv" | Where-Object { $_.Length -eq 22912911 } | Select-Object -First 1 -ExpandProperty FullName
$BuildRoot = Join-Path $env:TEMP "AFPIntegratedNativeBuild"
$DistDir = Join-Path $BuildRoot "dist"
$WorkDir = Join-Path $BuildRoot "work"

foreach ($Required in @($PythonExecutable, $LegacyCheckpoint, $LegacyReplayDir, $CausalArtifact, $LegacySource, $TrainingCore, (Join-Path $ModelRuntimeRoot "shijie"))) {
    if (-not (Test-Path -LiteralPath $Required)) { throw "Required asset missing: $Required" }
}
if (Test-Path -LiteralPath $BuildRoot) { Remove-Item -LiteralPath $BuildRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

& $PythonExecutable -m PyInstaller `
    --noconfirm --clean --onedir --noconsole `
    --name "AFP_Integrated_System" `
    --add-data "$AppDir\static;static" `
    --add-data "$AppDir\data;data" `
    --add-data "$AppDir\new_collection_demo_v11_3;new_collection_demo_v11_3" `
    --add-data "$LegacyReplayDir;data\legacy_replay" `
    --add-data "$CausalArtifact;data" `
    --add-data "$LegacyCheckpoint;models" `
    --add-data "$LegacySource;data" `
    --add-data "$ModelRuntimeRoot\shijie;model_runtime\shijie" `
    --add-data "$ModelRuntimeRoot\modern_TCN_models;model_runtime\modern_TCN_models" `
    --add-data "$TrainingCore;training_core" `
    --collect-all torch_geometric `
    --collect-submodules mysql.connector `
    --hidden-import sklearn.ensemble._forest `
    --hidden-import sklearn.linear_model._logistic `
    --hidden-import sklearn.svm._classes `
    --hidden-import sklearn.pipeline `
    --hidden-import sklearn.preprocessing._data `
    --hidden-import sklearn.metrics.cluster._expected_mutual_info_fast `
    --hidden-import app --hidden-import acquisition --hidden-import mysql_storage `
    --hidden-import native_integrated_app --hidden-import native_frontend_launcher `
    --hidden-import webview --hidden-import webview.platforms.winforms `
    --hidden-import online_inference --hidden-import online_health_features `
    --hidden-import causal_online_runtime --hidden-import runtime_scaler `
    --hidden-import new_collection_health --hidden-import runtime_health_primitives `
    --hidden-import web_training --hidden-import web_training_pipeline `
    --exclude-module PyQt5 --exclude-module IPython --exclude-module pytest `
    --exclude-module tensorflow --exclude-module tensorboard --exclude-module keras `
    --exclude-module paddle --exclude-module cv2 --exclude-module torchaudio `
    --distpath $DistDir --workpath $WorkDir --specpath $WorkDir `
    (Join-Path $AppDir "native_frontend_launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed: $LASTEXITCODE" }

$Built = Join-Path $DistDir "AFP_Integrated_System"
if (-not (Test-Path -LiteralPath (Join-Path $Built "AFP_Integrated_System.exe"))) { throw "Executable missing: $Built" }
if (Test-Path -LiteralPath $TargetDir) {
    $Backup = "$TargetDir`_backup_$(Get-Date -Format yyyyMMdd_HHmmss)"
    Move-Item -LiteralPath $TargetDir -Destination $Backup
}
Copy-Item -LiteralPath $Built -Destination $TargetDir -Recurse
$SourceTarget = Join-Path $TargetDir "source\visualization_app"
New-Item -ItemType Directory -Force -Path $SourceTarget | Out-Null
$SourceFiles = @(
    "native_frontend_launcher.py", "native_integrated_app.py", "app.py", "acquisition.py", "mysql_storage.py",
    "online_inference.py", "online_health_features.py", "causal_online_runtime.py",
    "runtime_scaler.py", "new_collection_health.py", "runtime_health_primitives.py",
    "web_training.py", "web_training_pipeline.py", "training_data.py",
    "build_native_integrated_app.ps1", "test_native_integrated_app.py"
)
foreach ($Name in $SourceFiles) {
    Copy-Item -LiteralPath (Join-Path $AppDir $Name) -Destination $SourceTarget
}
Copy-Item -LiteralPath (Join-Path $AppDir "static") -Destination $SourceTarget -Recurse
$ReadmeLines = @(
    "AFP Integrated Native System",
    "==================",
    "Start: AFP_Integrated_System.exe",
    "",
    "Pages:",
    "1. Acquisition, I-ModernTCN prediction, window/layer/specimen warning",
    "2. CSV/MySQL integration, I-ModernTCN training and warning training",
    "",
    "The original three-column frontend is embedded in the native window; no external browser or fixed localhost URL is needed.",
    "Keep the _internal folder beside the executable."
)
$ReadmeLines | Set-Content -LiteralPath (Join-Path $TargetDir "README.txt") -Encoding UTF8
Write-Host "Built and copied to: $TargetDir"
