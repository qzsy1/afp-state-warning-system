[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReferenceRelease,
    [Parameter(Mandatory = $true)][string]$TargetDir,
    [Parameter(Mandatory = $true)][string]$ApplicationVersion,
    [string]$LauncherSourceDir = "",
    [string]$ExistingExecutable = "",
    [string]$LauncherVersion = "",
    [switch]$AllowExistingTarget
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$LegacySource = Join-Path $RepoRoot "visualization_app"
$referenceInternal = Join-Path $ReferenceRelease "_internal"
$referenceLegacy = Join-Path $ReferenceRelease "app\legacy"
$referenceData = Join-Path $referenceInternal "data"
if (-not (Test-Path -LiteralPath $referenceData) -and (Test-Path -LiteralPath (Join-Path $referenceLegacy "data"))) {
    $referenceData = Join-Path $referenceLegacy "data"
}

if ($LauncherSourceDir -and $ExistingExecutable) {
    throw "Specify LauncherSourceDir or ExistingExecutable, not both."
}
if (-not $LauncherSourceDir -and -not $ExistingExecutable) {
    throw "A launcher source directory or existing executable is required."
}

$resolvedLauncherVersion = $LauncherVersion.Trim()
if (-not $resolvedLauncherVersion) {
    $launcherVersionRoot = if ($ExistingExecutable) {
        Split-Path -Parent $ExistingExecutable
    } else {
        $LauncherSourceDir
    }
    $launcherVersionPath = Join-Path $launcherVersionRoot "VERSION.json"
    if (Test-Path -LiteralPath $launcherVersionPath -PathType Leaf) {
        $sourceVersion = Get-Content -LiteralPath $launcherVersionPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $resolvedLauncherVersion = [string]$sourceVersion.launcher_version
    }
}
if (-not $resolvedLauncherVersion) {
    # A freshly built launcher has no VERSION.json yet, so its version follows this release.
    $resolvedLauncherVersion = $ApplicationVersion
}

foreach ($required in @(
    $ReferenceRelease,
    (Join-Path $ScriptDir "app\bootstrap.py"),
    (Join-Path $LegacySource "app.py"),
    $referenceData,
    (Join-Path $ReferenceRelease "models")
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required delivery assembly input is missing: $required"
    }
}

if ((Test-Path -LiteralPath $TargetDir) -and -not $AllowExistingTarget) {
    throw "Target already exists; choose a new directory: $TargetDir"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetDir), $TargetDir | Out-Null

if ($LauncherSourceDir) {
    $launcherExecutable = Join-Path $LauncherSourceDir "AFP_Integrated_System_Modular.exe"
    if (-not (Test-Path -LiteralPath $launcherExecutable -PathType Leaf)) {
        throw "Launcher source is missing its executable: $LauncherSourceDir"
    }
    Get-ChildItem -LiteralPath $LauncherSourceDir -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $TargetDir -Recurse -Force
    }
} else {
    if (-not (Test-Path -LiteralPath $ExistingExecutable -PathType Leaf)) {
        throw "Existing executable is missing: $ExistingExecutable"
    }
    $targetExecutable = Join-Path $TargetDir "AFP_Integrated_System_Modular.exe"
    if ([IO.Path]::GetFullPath($ExistingExecutable) -ne [IO.Path]::GetFullPath($targetExecutable)) {
        Copy-Item -LiteralPath $ExistingExecutable -Destination $targetExecutable -Force
    }
    $existingInternal = Join-Path (Split-Path -Parent $ExistingExecutable) "_internal"
    $targetInternal = Join-Path $TargetDir "_internal"
    if (-not (Test-Path -LiteralPath $existingInternal -PathType Container)) {
        throw "Existing launcher runtime directory is missing: $existingInternal"
    }
    if ([IO.Path]::GetFullPath($existingInternal) -ne [IO.Path]::GetFullPath($targetInternal)) {
        New-Item -ItemType Directory -Force -Path $targetInternal | Out-Null
        Get-ChildItem -LiteralPath $existingInternal -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $targetInternal -Recurse -Force
        }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $TargetDir "AFP_Integrated_System_Modular.exe") -PathType Leaf)) {
    throw "Assembled delivery is missing AFP_Integrated_System_Modular.exe: $TargetDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $TargetDir "_internal") -PathType Container)) {
    throw "Assembled delivery is missing the launcher runtime directory: $TargetDir"
}

$appTarget = Join-Path $TargetDir "app"
$legacyTarget = Join-Path $appTarget "legacy"
$legacyStaticTarget = Join-Path $legacyTarget "static"
$uiTarget = Join-Path $appTarget "ui"
$configTarget = Join-Path $TargetDir "config"
$nativeTarget = Join-Path $TargetDir "native_dll"
$docsTarget = Join-Path $TargetDir "docs"
foreach ($path in @($appTarget, $legacyTarget, $legacyStaticTarget, $uiTarget, $configTarget, $nativeTarget, $docsTarget)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

Copy-Item -LiteralPath (Join-Path $ScriptDir "app\bootstrap.py") -Destination $appTarget -Force
New-Item -ItemType Directory -Force -Path (Join-Path $appTarget "core"), (Join-Path $appTarget "modules") | Out-Null
Copy-Item -Path (Join-Path $ScriptDir "app\core\*") -Destination (Join-Path $appTarget "core") -Recurse -Force
Copy-Item -Path (Join-Path $ScriptDir "app\modules\*") -Destination (Join-Path $appTarget "modules") -Recurse -Force
$runtimeConfigPath = Join-Path $configTarget "runtime.json"
Copy-Item -LiteralPath (Join-Path $ScriptDir "config\runtime.delivery.json") -Destination $runtimeConfigPath -Force
$runtimeConfig = Get-Content -LiteralPath $runtimeConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$runtimeConfig.application_version = $ApplicationVersion
$runtimeConfig | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $runtimeConfigPath -Encoding UTF8
$documentation = Get-ChildItem -LiteralPath (Join-Path $RepoRoot "docs") -Filter "*.md" -File
if (-not $documentation) {
    throw "Modular documentation directory does not contain Markdown files."
}
$documentation | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $docsTarget -Force
}
$operationalFiles = @(
    "install_local_helper_autostart.ps1",
    "public_tunnel_watchdog.ps1",
    "tailscale_funnel_watchdog.ps1"
)
foreach ($name in $operationalFiles) {
    $source = Join-Path $LegacySource $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Release operational script is missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $TargetDir $name) -Force
}

$legacyFiles = @(
    "app.py", "interface_agent.py", "agentic_diagnosis.py", "diagnostic_tools.py", "acquisition.py", "smrf_hid.py", "mysql_storage.py",
    "online_inference.py", "atavn.py", "online_health_features.py",
    "causal_online_runtime.py", "runtime_scaler.py", "new_collection_health.py",
    "runtime_health_primitives.py", "web_training.py", "web_training_pipeline.py",
    "training_data.py", "training_center.py", "training_center_cli.py",
    "native_integrated_app.py", "fit_new_collection_health.py", "websocket_live.py",
    "helper_relay.py", "edge_capture.py", "local_capture_agent.py", "local_capture_helper_entry.py", "windows_usb_topology.py", "interface_transport_catalog.py",
    "simulation_replay.py", "simulation_source_transfer.py",
    "public_demo_bundles.py",
    "remote_mysql_setup.py", "server_target_mysql.py", "server_capture_journal.py", "generate_pressure_simulation.py",
    "web_auth.py", "web_access.py", "public_status.py", "guest_simulation.py", "control_lease.py", "json_safety.py",
    "diagnosis_jobs.py", "simulation_packages.py"
)
foreach ($name in $legacyFiles) {
    $source = Join-Path $LegacySource $name
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Legacy compatibility source missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination $legacyTarget -Force
}
foreach ($name in @(
    "web_auth.py", "web_access.py", "public_status.py", "guest_simulation.py",
    "control_lease.py", "json_safety.py", "simulation_replay.py", "simulation_source_transfer.py"
)) {
    $copied = Join-Path $legacyTarget $name
    if (-not (Test-Path -LiteralPath $copied)) {
        throw "Public-web compatibility source was not copied: $copied"
    }
}
Get-ChildItem -LiteralPath (Join-Path $LegacySource "static") -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $legacyStaticTarget -Recurse -Force
    Copy-Item -LiteralPath $_.FullName -Destination $uiTarget -Recurse -Force
}
$simulationPackageSource = Join-Path $LegacySource "simulation_packages"
$simulationPackageTarget = Join-Path $legacyTarget "simulation_packages"
if (-not (Test-Path -LiteralPath $simulationPackageSource -PathType Container)) {
    throw "Simulation package directory is missing: $simulationPackageSource"
}
New-Item -ItemType Directory -Force -Path $simulationPackageTarget | Out-Null
Get-ChildItem -LiteralPath $simulationPackageSource -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $simulationPackageTarget -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir "models"), (Join-Path $legacyTarget "data") | Out-Null
Copy-Item -Path (Join-Path $ReferenceRelease "models\*") -Destination (Join-Path $TargetDir "models") -Recurse -Force
Copy-Item -Path (Join-Path $referenceData "*") -Destination (Join-Path $legacyTarget "data") -Recurse -Force
if (Test-Path -LiteralPath (Join-Path $referenceData "new_collection_demo_v11_3")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "new_collection_demo_v11_3") | Out-Null
    Copy-Item -Path (Join-Path $referenceData "new_collection_demo_v11_3\*") -Destination (Join-Path $legacyTarget "new_collection_demo_v11_3") -Recurse -Force
} elseif (Test-Path -LiteralPath (Join-Path $referenceLegacy "new_collection_demo_v11_3")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "new_collection_demo_v11_3") | Out-Null
    Copy-Item -Path (Join-Path $referenceLegacy "new_collection_demo_v11_3\*") -Destination (Join-Path $legacyTarget "new_collection_demo_v11_3") -Recurse -Force
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "new_collection_demo_v11_3") | Out-Null
    Copy-Item -Path (Join-Path $LegacySource "new_collection_demo_v11_3\*") -Destination (Join-Path $legacyTarget "new_collection_demo_v11_3") -Recurse -Force
}
if (Test-Path -LiteralPath (Join-Path $referenceData "model_runtime")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "model_runtime") | Out-Null
    Copy-Item -Path (Join-Path $referenceData "model_runtime\*") -Destination (Join-Path $legacyTarget "model_runtime") -Recurse -Force
} elseif (Test-Path -LiteralPath (Join-Path $referenceLegacy "model_runtime")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "model_runtime") | Out-Null
    Copy-Item -Path (Join-Path $referenceLegacy "model_runtime\*") -Destination (Join-Path $legacyTarget "model_runtime") -Recurse -Force
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyTarget "model_runtime") | Out-Null
    Copy-Item -Path (Join-Path $LegacySource "model_runtime\*") -Destination (Join-Path $legacyTarget "model_runtime") -Recurse -Force
}
Get-ChildItem -LiteralPath (Join-Path $LegacySource "hardware_dlls") -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $nativeTarget -Force
}

foreach ($directory in @("logs", "runtime", "rollback", "updates", "verification")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir $directory) | Out-Null
}

# Delivery trees must never contain runtime credentials or tunnel secrets.
$mutableDirectories = @("logs", "runtime", "rollback", "updates", "verification")
$forbiddenNames = Get-ChildItem -LiteralPath $TargetDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($TargetDir.Length).TrimStart('\')
    $segments = $relative -split '[\\/]'
    $topDirectory = $segments[0]
    $nestedRuntime = $segments.Count -ge 2 -and $segments[0] -eq "app" -and $segments[1] -eq "runtime"
    $topDirectory -notin $mutableDirectories -and -not $nestedRuntime -and (
        $_.Name -in @("public_web_security.sqlite3", "cert.pem") -or
        $_.Name -like "*.cfargotunnel.com.json"
    )
}
if ($forbiddenNames) {
    throw "Delivery contains a forbidden public-web secret file: $($forbiddenNames[0].FullName)"
}
$textExtensions = @(".py", ".ps1", ".json", ".txt", ".md", ".html", ".js", ".css", ".toml", ".yaml", ".yml")
foreach ($file in (Get-ChildItem -LiteralPath $TargetDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($TargetDir.Length).TrimStart('\')
    $segments = $relative -split '[\\/]'
    $topDirectory = $segments[0]
    $nestedRuntime = $segments.Count -ge 2 -and $segments[0] -eq "app" -and $segments[1] -eq "runtime"
    $topDirectory -notin $mutableDirectories -and -not $nestedRuntime -and $textExtensions -contains $_.Extension.ToLowerInvariant()
})) {
    $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop
    if ($text -match 'sk-[A-Za-z0-9_-]{20,}') {
        throw "Delivery text contains a SiliconFlow-style API key: $($file.FullName)"
    }
}

@{
    application_version = $ApplicationVersion
    launcher_version = $resolvedLauncherVersion
    module_api_version = "2.0"
    baseline = "v1.12.1-r3"
    built_at = (Get-Date).ToString("o")
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $TargetDir "VERSION.json") -Encoding UTF8

@(
    "AFP Integrated System Modular v$ApplicationVersion",
    "====================================",
    "Start: AFP_Integrated_System_Modular.exe",
    "Application logic is stored in app/modules and app/legacy and can be updated without rebuilding the EXE.",
    "UI files are in app/ui; configuration is in config/runtime.json; prediction weights are in models.",
    "Keep the complete directory together.  Another PC does not need Python, PyTorch or Git.",
    "Use --module-status or --self-test for diagnostics; use --verify-files for SHA-256 integrity verification.",
    "Use --mysql-smoke and --spreadsheet-smoke to verify database and Excel runtime dependencies.",
    "Use verified patch ZIP files for module updates. Mutable logs, runtime data, update packages, verification outputs and Python caches are not in SHA256SUMS.txt."
) | Set-Content -LiteralPath (Join-Path $TargetDir "README.txt") -Encoding UTF8

function Get-Sha256Hex([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "")
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

$immutableFiles = Get-ChildItem -LiteralPath $TargetDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($TargetDir.Length).TrimStart('\')
    $topDirectory = ($relative -split '[\\/]')[0]
    $segments = $relative -split '[\\/]'
    $nestedRuntime = $segments.Count -ge 2 -and $segments[0] -eq "app" -and $segments[1] -eq "runtime"
    $_.Name -ne "SHA256SUMS.txt" -and
        $topDirectory -notin $mutableDirectories -and
        -not $nestedRuntime -and
        $_.Extension -ne ".pyc" -and
        "__pycache__" -notin $segments
}
$immutableFiles | ForEach-Object {
    $relative = $_.FullName.Substring($TargetDir.Length).TrimStart('\')
    "$(Get-Sha256Hex $_.FullName) *$relative"
} | Set-Content -LiteralPath (Join-Path $TargetDir "SHA256SUMS.txt") -Encoding UTF8

Write-Host "Modular delivery assembled: $TargetDir"
