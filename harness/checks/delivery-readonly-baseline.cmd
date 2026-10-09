@echo off
setlocal
echo AFP Harness launcher started: delivery-readonly-baseline
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\engine\run.ps1" -Check delivery-readonly-baseline
set "HARNESS_RC=%ERRORLEVEL%"
if not "%HARNESS_RC%"=="0" pause
exit /b %HARNESS_RC%
