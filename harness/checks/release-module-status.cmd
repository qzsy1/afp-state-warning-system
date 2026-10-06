@echo off
chcp 65001 >nul
setlocal
echo AFP Harness launcher started. Please wait...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\engine\run.ps1" -Check release-module-status -OpenReport
set "HARNESS_RC=%ERRORLEVEL%"
echo.
pause
exit /b %HARNESS_RC%
