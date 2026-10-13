param(
    [string]$PythonExecutable = "py",
    [string]$DeliveryRoot = "",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $PSScriptRoot "local_capture_helper_entry.py"
if ($OutputDirectory -and $DeliveryRoot) {
    throw "Specify OutputDirectory or DeliveryRoot, not both."
}
if (-not $OutputDirectory -and -not $DeliveryRoot) {
    $DeliveryRoot = Join-Path $root "delivery\AFP_Integrated_System_Modular_v2.0.4_Agentic"
}
$delivery = if ($OutputDirectory) {
    [IO.Path]::GetFullPath($OutputDirectory)
} else {
    Join-Path $DeliveryRoot "local_helper"
}
$watchdogSource = Join-Path $PSScriptRoot "tailscale_funnel_watchdog.ps1"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("afp-local-helper-build-" + [guid]::NewGuid().ToString("N"))

if (-not (Test-Path -LiteralPath $entry)) {
    throw "找不到本地辅助程序入口：$entry"
}
$pythonCommand = $PythonExecutable
$pythonPrefix = @()
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    $pythonPrefix = @("-3.11")
}
& $pythonCommand @pythonPrefix -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "当前Python环境未安装PyInstaller；先安装后再构建本地辅助程序"
}

New-Item -ItemType Directory -Path $staging | Out-Null
try {
    & $pythonCommand @pythonPrefix -m PyInstaller --noconfirm --clean --onefile --console `
        --hidden-import websocket --name "AFP_Local_Capture_Helper" --distpath $staging --workpath (Join-Path $staging "build") `
        --specpath $staging $entry
    if ($LASTEXITCODE -ne 0) {
        throw "本地辅助程序PyInstaller构建失败：$LASTEXITCODE"
    }

    if (-not (Test-Path -LiteralPath $delivery)) {
        New-Item -ItemType Directory -Path $delivery -Force | Out-Null
    }
    $target = Join-Path $delivery "AFP_Local_Capture_Helper.exe"
    Copy-Item -LiteralPath (Join-Path $staging "AFP_Local_Capture_Helper.exe") -Destination $target -Force

    if ($DeliveryRoot) {
        Copy-Item -LiteralPath $watchdogSource -Destination (Join-Path $DeliveryRoot "tailscale_funnel_watchdog.ps1") -Force
        $mutableDirectories = @("logs", "runtime", "rollback", "updates", "verification")
        $hashes = Get-ChildItem -LiteralPath $DeliveryRoot -Recurse -File | Where-Object {
            $relative = $_.FullName.Substring($DeliveryRoot.Length).TrimStart('\')
            $segments = $relative -split '[\\/]'
            $topDirectory = $segments[0]
            $nestedRuntime = $segments.Count -ge 2 -and $segments[0] -eq "app" -and $segments[1] -eq "runtime"
            $_.Name -ne "SHA256SUMS.txt" -and
                $topDirectory -notin $mutableDirectories -and
                -not $nestedRuntime -and
                $_.Extension -ne ".pyc" -and
                "__pycache__" -notin $segments
        } | Get-FileHash -Algorithm SHA256
        $hashes | ForEach-Object {
            $relative = $_.Path.Substring($DeliveryRoot.Length).TrimStart('\')
            "$($_.Hash) *$relative"
        } | Set-Content -LiteralPath (Join-Path $DeliveryRoot "SHA256SUMS.txt") -Encoding UTF8
    }
    Write-Output $target
}
finally {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $resolvedStaging = [IO.Path]::GetFullPath($staging)
    if ($resolvedStaging.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedStaging).StartsWith("afp-local-helper-build-", [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedStaging)) {
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
