@echo off
chcp 65001 >nul
setlocal
set "TEMP=D:\AFP_Runtime\.afp-staging\acquisition-pause\temp"
set "TMP=%TEMP%"
set "PYTHONPYCACHEPREFIX=D:\AFP_Runtime\.afp-staging\acquisition-pause\pycache"
set "AFP_CAPTURE_ROOT=D:\AFP_Runtime\.afp-staging\acquisition-pause\default-capture"
set "AFP_RUNTIME_DIR=D:\AFP_Runtime\.afp-staging\acquisition-pause\runtime"
if not exist "%TEMP%" mkdir "%TEMP%"
echo AFP Harness launcher started. Please wait...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\engine\run.ps1" -Check acquisition-pause-contracts -OpenReport
set "HARNESS_RC=%ERRORLEVEL%"
echo.
pause
exit /b %HARNESS_RC%
