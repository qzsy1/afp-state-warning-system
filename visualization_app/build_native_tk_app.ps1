param(
    [string]$PythonExecutable = "",
    [string]$TargetDir = ""
)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = $PythonExecutable
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = "F:\program\channel_independent_MTSF-main\.venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}
if ([string]::IsNullOrWhiteSpace($TargetDir)) {
    $TargetDir = Join-Path $AppDir "release_native_tk_fix"
}
$ExternalRoot = "F:\program\XJUsorceopen"
$LegacyReplayDir = Join-Path (Split-Path -Parent $AppDir) "outputs_tc_hi_soft_consistency_v13_8"
$CausalArtifact = Join-Path (Split-Path -Parent $AppDir) "outputs_causal_online_consistency_v13_9\causal_online_consistency_artifact.joblib"
$BuildRoot = Join-Path $env:TEMP ("AFPNativeTkBuild_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$DistDir = Join-Path $BuildRoot "dist"
$WorkDir = Join-Path $BuildRoot "work"
$SpecDir = Join-Path $BuildRoot "spec"
New-Item -ItemType Directory -Force -Path $BuildRoot, $DistDir, $WorkDir, $SpecDir | Out-Null

$PyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onedir", "--noconsole",
    "--name", "AFP_State_Warning_Native",
    "--paths", $AppDir, "--paths", $ExternalRoot,
    "--add-data", "$AppDir\data;data",
    "--add-data", "$LegacyReplayDir;data\legacy_replay",
    "--add-data", "$CausalArtifact;data",
    "--add-data", "$AppDir\models;models",
    "--add-data", "$AppDir\new_collection_demo_v11_3;new_collection_demo_v11_3",
    "--add-data", "$AppDir\trained_models_web;trained_models_web",
    "--add-data", "$ExternalRoot\shijie;model_runtime\shijie",
    "--add-data", "$ExternalRoot\modern_TCN_models;model_runtime\modern_TCN_models",
    "--hidden-import", "app", "--hidden-import", "acquisition",
    "--hidden-import", "mysql_storage", "--hidden-import", "online_inference",
    "--hidden-import", "atavn", "--hidden-import", "online_health_features",
    "--hidden-import", "causal_online_runtime", "--hidden-import", "runtime_scaler",
    "--hidden-import", "new_collection_health", "--hidden-import", "runtime_health_primitives",
    "--hidden-import", "web_training", "--hidden-import", "web_training_pipeline",
    "--hidden-import", "training_data", "--hidden-import", "fit_new_collection_health",
    "--hidden-import", "sklearn.ensemble._forest",
    "--hidden-import", "sklearn.linear_model._logistic",
    "--hidden-import", "sklearn.svm._classes",
    "--hidden-import", "sklearn.metrics.cluster._expected_mutual_info_fast",
    "--collect-submodules", "mysql.connector",
    "--collect-all", "torch_geometric",
    "--exclude-module", "PyQt5", "--exclude-module", "IPython",
    "--exclude-module", "pytest", "--exclude-module", "tensorflow",
    "--exclude-module", "tensorboard", "--exclude-module", "keras",
    "--exclude-module", "paddle", "--exclude-module", "cv2",
    "--exclude-module", "torchaudio",
    "--distpath", $DistDir, "--workpath", $WorkDir, "--specpath", $SpecDir,
    (Join-Path $AppDir "native_integrated_app.py")
)
& $Python @PyArgs
if ($LASTEXITCODE -ne 0) {
    throw "Native Tk PyInstaller build failed: $LASTEXITCODE"
}
$Built = Join-Path $DistDir "AFP_State_Warning_Native"
$Exe = Join-Path $Built "AFP_State_Warning_Native.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Native executable was not produced: $Exe"
}
# Keep the destination directory in place.  Windows may reject deleting a
# deeply nested scientific-runtime file; copying over the existing package is
# safer and still replaces every file produced by the current build.
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Copy-Item -Path (Join-Path $Built "*") -Destination $TargetDir -Recurse -Force
$Readme = @(
    "AFP_State_Warning_Native",
    "Native Tk desktop version; it does not open a web page or external browser.",
    "Start AFP_State_Warning_Native.exe and keep _internal beside it.",
    "The new collection scheme contains 16 physical sensor channels and 4 process parameters.",
    "The desktop tabs provide acquisition, live prediction/warning, MySQL saving/relations, and model training.",
    "The acquisition channel list is schema-driven; new_collection_v11_3 displays exactly 16 sensor channels.",
    "The actual curve is shown immediately; the first forecast appears after 24 valid samples and the first complete warning window after the model window is complete.",
    "For a source rebuild, run build_native_tk_app.ps1 from the visualization_app directory.",
    "--self-test checks the packaged data and model resources without opening the UI."
)
$Readme | Set-Content -LiteralPath (Join-Path $TargetDir "README.txt") -Encoding UTF8
Write-Host "Native application created: $TargetDir"
