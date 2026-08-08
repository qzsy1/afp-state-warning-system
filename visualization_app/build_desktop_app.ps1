$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateMonitorDir = Split-Path -Parent $AppDir
$ProjectRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $StateMonitorDir))
$WorkspaceRoot = Split-Path -Parent $ProjectRoot
$Python = Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"
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
$ReleaseDir = Join-Path $AppDir "release\AFP_State_Warning_System"

foreach ($Required in @($LegacyCheckpoint, $LegacyReplayDir, $CausalArtifact, $LegacySource, $ModelRuntimeShijie, $ModelRuntimeModernTCN)) {
    if (-not (Test-Path -LiteralPath $Required)) { throw "Required runtime asset is missing: $Required" }
}

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
    --exclude-module PyQt5 `
    --exclude-module IPython `
    --exclude-module pytest `
    --exclude-module tensorflow `
    --exclude-module tensorboard `
    --exclude-module keras `
    --exclude-module paddle `
    --exclude-module cv2 `
    --exclude-module torchaudio `
    --exclude-module matplotlib `
    --distpath (Join-Path $AppDir "release") `
    --workpath (Join-Path $AppDir "desktop_build") `
    --specpath (Join-Path $AppDir "desktop_build") `
    (Join-Path $AppDir "desktop_launcher.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}
Write-Host "Portable desktop application created:" $ReleaseDir
