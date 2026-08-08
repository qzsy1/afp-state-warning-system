@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=F:\program\channel_independent_MTSF-main\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

if not exist "data\dashboard_sequences.npz" (
  echo [1/2] Preparing AFP dashboard data...
  "%PYTHON%" prepare_dashboard_data.py
  if errorlevel 1 (
    echo Data preparation failed.
    pause
    exit /b 1
  )
)

if not exist "data\dashboard_candidate_features.npz" (
  echo [2/3] Preparing 48 indicator-model candidate scores...
  "%PYTHON%" prepare_dashboard_candidate_scores.py
  if errorlevel 1 (
    echo Candidate score preparation failed.
    pause
    exit /b 1
  )
)
if not exist "data\candidate_models\candidate_47.joblib" (
  echo [2/3] Preparing realtime anomaly-score models...
  "%PYTHON%" prepare_dashboard_candidate_scores.py
  if errorlevel 1 (
    echo Realtime anomaly model preparation failed.
    pause
    exit /b 1
  )
)
if not exist "data\online_feature_artifacts.joblib" (
  echo [2/3] Preparing realtime health-indicator transformers...
  "%PYTHON%" prepare_dashboard_candidate_scores.py
  if errorlevel 1 (
    echo Realtime health-indicator preparation failed.
    pause
    exit /b 1
  )
)
if not exist "..\outputs_causal_online_consistency_v13_9\causal_online_consistency_artifact.joblib" (
  echo [2/3] Preparing causal online consistency model...
  "%PYTHON%" ..\run_causal_online_consistency_v13_9.py
  if errorlevel 1 (
    echo Causal online consistency preparation failed.
    pause
    exit /b 1
  )
)
if not exist "new_collection_demo_v11_3\models\new_collection_hi_artifacts.joblib" (
  echo [2/3] Fitting 12 new-dataset health indicators x 4 classifiers...
  "%PYTHON%" fit_new_collection_health.py
  if errorlevel 1 (
    echo New-dataset health-indicator fitting failed.
    echo You can retry by double-clicking fit_new_collection_health.bat.
    pause
    exit /b 1
  )
)

echo [3/3] Starting AFP dashboard at http://127.0.0.1:8765
"%PYTHON%" app.py
endlocal
