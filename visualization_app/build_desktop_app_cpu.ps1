param(
    [string]$PythonExecutable = "C:\Users\xlq\AppData\Local\Temp\AFPBuildCPU\Scripts\python.exe",
    [switch]$SkipArchive
)
$ErrorActionPreference = "Stop"
$script = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "build_desktop_app.ps1"
if (-not (Test-Path -LiteralPath $PythonExecutable)) {
    throw "CPU Python environment not found: $PythonExecutable"
}
& powershell -NoProfile -ExecutionPolicy Bypass -File $script -PythonExecutable $PythonExecutable -SkipArchive:$SkipArchive
if ($LASTEXITCODE -ne 0) { throw "CPU portable build failed with exit code $LASTEXITCODE" }
