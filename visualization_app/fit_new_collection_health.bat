@echo off
setlocal
cd /d "%~dp0"
"F:\program\channel_independent_MTSF-main\.venv\Scripts\python.exe" fit_new_collection_health.py
if errorlevel 1 (
  echo.
  echo [FAILED] New collection health-indicator fitting failed.
  pause
  exit /b 1
)
echo.
echo [OK] 12 indicators x 4 models were fitted from the new dataset.
pause
