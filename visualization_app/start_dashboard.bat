@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo Starting AFP dashboard from %CD%
"%PYTHON%" app.py --host 127.0.0.1 --port 8765
if errorlevel 1 (
  echo AFP dashboard failed to start. Check the Python error above and confirm port 8765 is available.
  pause
  exit /b 1
)
endlocal
