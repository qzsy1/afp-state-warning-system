@echo off
chcp 65001 >nul
setlocal
echo AFP Harness launcher started. Please wait...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine\run.ps1" -Setup
set "HARNESS_RC=%ERRORLEVEL%"
echo.
pause
exit /b %HARNESS_RC%
