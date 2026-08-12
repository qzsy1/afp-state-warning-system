param(
    [string]$PythonExecutable = "",
    [switch]$SkipArchive
)
$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateMonitorDir = Split-Path -Parent $AppDir
$ProjectRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $StateMonitorDir))
$WorkspaceRoot = Split-Path -Parent $ProjectRoot
$Python = $PythonExecutable
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }

$LegacyCheckpoint = Join-Path $ProjectRoot "checkpoints\health_i_T_G_MyCustom_ftM_sl24_ll24_pl24_dm128_nh8_el2_dl1_df2048_fc1_ebtimeF_dtTrue_health_v9_conditional_normal_no_param_score_lr0.001_bs128\checkpoint.pth"
$LegacyReplayDir = Join-Path $StateMonitorDir "outputs_tc_hi_soft_consistency_v13_8"
$CausalArtifact = Join-Path $StateMonitorDir "outputs_causal_online_consistency_v13_9\causal_online_consistency_artifact.joblib"
$ModelRuntimeRoot = "F:\program\XJUsorceopen"
$ModelRuntimeShijie = Join-Path $ModelRuntimeRoot "shijie"
$ModelRuntimeModernTCN = Join-Path $ModelRuntimeRoot "modern_TCN_models"
$LegacySource = (
    Get-ChildItem -LiteralPath (Split-Path -Parent $StateMonitorDir) -File -Filter "*.csv" |
        Where-Object { $_.Length -eq 22912911 } |
        Select-Object -First 1 -ExpandProperty FullName
)
$ReleaseDir = Join-Path $AppDir "release"
$ZipPath = Join-Path $ReleaseDir "AFP_State_Warning_System_Windows.zip"
# PyInstaller expands dependency paths below the output directory.  A short
# temporary path avoids Windows MAX_PATH failures for scientific DLL names.
$BuildRoot = Join-Path $env:TEMP "AFPBuild"
$DistDir = Join-Path $BuildRoot "dist"
$WorkDir = Join-Path $BuildRoot "work"

foreach ($Required in @($LegacyCheckpoint, $LegacyReplayDir, $CausalArtifact, $LegacySource, $ModelRuntimeShijie, $ModelRuntimeModernTCN)) {
    if (-not (Test-Path -LiteralPath $Required)) { throw "Required runtime asset is missing: $Required" }
}

if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
New-Item -ItemType Directory -Force -Path $BuildRoot, $ReleaseDir | Out-Null

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --noconsole `
    --name "AFP_State_Warning_System" `
    --add-data "$AppDir\static;static" `
    --add-data "$AppDir\data;data" `
    --add-data "$AppDir\new_collection_demo_v11_3;new_collection_demo_v11_3" `
    --add-data "$LegacyReplayDir;data\legacy_replay" `
    --add-data "$CausalArtifact;data" `
    --add-data "$LegacyCheckpoint;models" `
    --add-data "$LegacySource;data" `
    --add-data "$ModelRuntimeShijie;model_runtime\shijie" `
    --add-data "$ModelRuntimeModernTCN;model_runtime\modern_TCN_models" `
    --collect-all torch_geometric `
    --hidden-import sklearn.ensemble._forest `
    --hidden-import sklearn.linear_model._logistic `
    --hidden-import sklearn.svm._classes `
    --hidden-import sklearn.pipeline `
    --hidden-import sklearn.preprocessing._data `
    --exclude-module PyQt5 `
    --exclude-module IPython `
    --exclude-module pytest `
    --exclude-module tensorflow `
    --exclude-module tensorboard `
    --exclude-module keras `
    --exclude-module paddle `
    --exclude-module cv2 `
    --exclude-module torchaudio `
    --distpath $DistDir `
    --workpath $WorkDir `
    --specpath $WorkDir `
    (Join-Path $AppDir "desktop_launcher.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$BuiltApp = Join-Path $DistDir "AFP_State_Warning_System"
if (-not (Test-Path -LiteralPath (Join-Path $BuiltApp "AFP_State_Warning_System.exe"))) {
    throw "The desktop executable was not produced: $BuiltApp"
}
if ($SkipArchive) {
    Write-Host "Portable desktop application created:" $BuiltApp
}
else {
    Compress-Archive -Path (Join-Path $BuiltApp "*") -DestinationPath $ZipPath -CompressionLevel Optimal
    Write-Host "Portable desktop application archive created:" $ZipPath
}
