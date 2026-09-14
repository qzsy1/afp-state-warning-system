param(
    [string]$PythonExecutable = "py"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $PSScriptRoot "local_capture_helper_entry.py"
$delivery = Join-Path $root "delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic\local_helper"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) "afp-local-helper-build"

if (-not (Test-Path -LiteralPath $entry)) {
    throw "找不到本地辅助程序入口：$entry"
}
& $PythonExecutable -3.11 -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "当前Python环境未安装PyInstaller；先安装后再构建本地辅助程序"
}

if (Test-Path -LiteralPath $staging) {
    Remove-Item -LiteralPath $staging -Recurse -Force
}
New-Item -ItemType Directory -Path $staging | Out-Null
& $PythonExecutable -3.11 -m PyInstaller --noconfirm --clean --onefile --console `
    --name "AFP_Local_Capture_Helper" --distpath $staging --workpath (Join-Path $staging "build") `
    --specpath $staging $entry
if ($LASTEXITCODE -ne 0) {
    throw "本地辅助程序PyInstaller构建失败：$LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $delivery)) {
    New-Item -ItemType Directory -Path $delivery | Out-Null
}
$target = Join-Path $delivery "AFP_Local_Capture_Helper.exe"
Copy-Item -LiteralPath (Join-Path $staging "AFP_Local_Capture_Helper.exe") -Destination $target -Force
Write-Host "已更新本地辅助程序：$target"
