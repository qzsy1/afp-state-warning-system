[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$LabRoot = "F:\AFP_Client_Validation_Lab",
    [string]$HelperSource = "",
    [string]$FixtureSource = "F:\AFP_Capture\simulation_m3232_new_collection\SIM_PRESSURE_M3232_new_collection.csv",
    [string]$MySqlSource = "F:\softwawre\mysql",
    [string]$VcRedistSource = "",
    [switch]$DescribeContract
)

$ErrorActionPreference = "Stop"

$ExpectedLabRoot = "F:\AFP_Client_Validation_Lab"
$ExpectedHelperHash = "5216A6D110164BB23D8D1B2D29DBB335D998DA1DB176CB227339054586BCA5D6"
$ExpectedFixtureHash = "58C9290EFD79B8967DD4806FE9CA13B15B4E5508835D3F019A5E943BADD78267"
$AllowedMySqlEntries = @("bin", "lib", "share", "LICENSE")
$PrerequisiteEntries = @("vc_redist.x64.exe")
$ExcludedNames = @("data", "my.ini", "runtime", "logs", "*.sqlite3", "config.json")

if ($DescribeContract) {
    [ordered]@{
        lab_root = $ExpectedLabRoot
        helper_sha256 = $ExpectedHelperHash
        fixture_sha256 = $ExpectedFixtureHash
        mysql_entries = $AllowedMySqlEntries
        prerequisite_entries = $PrerequisiteEntries
        prerequisite_signature_required = $true
        excluded_names = $ExcludedNames
        input_read_only = $true
        results_read_only = $false
    } | ConvertTo-Json -Depth 4
    return
}

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "")
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Assert-FileHash {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label input is missing: $Path"
    }
    $actual = (Get-Sha256Hex -Path $Path).ToUpperInvariant()
    if ($actual -ne $Expected) {
        throw "$Label SHA-256 mismatch: expected $Expected, actual $actual"
    }
    return $actual
}

function Assert-MicrosoftSignature {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "$Label Authenticode signature is not valid: $($signature.Status)"
    }
    if (-not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
        throw "$Label signer is not Microsoft Corporation"
    }
}

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
}
$resolvedRepoRoot = Get-NormalizedPath -Path $RepoRoot
$resolvedLabRoot = Get-NormalizedPath -Path $LabRoot
if ($resolvedLabRoot -ine $ExpectedLabRoot) {
    throw "LabRoot must be exactly $ExpectedLabRoot; refusing target: $resolvedLabRoot"
}
if ($resolvedLabRoot -ieq $resolvedRepoRoot -or $resolvedRepoRoot.StartsWith($resolvedLabRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "LabRoot must not contain the repository: $resolvedRepoRoot"
}

if ([string]::IsNullOrWhiteSpace($HelperSource)) {
    $HelperSource = Join-Path $resolvedRepoRoot "delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic\local_helper\AFP_Local_Capture_Helper.exe"
}
$resolvedHelperSource = Get-NormalizedPath -Path $HelperSource
$resolvedFixtureSource = Get-NormalizedPath -Path $FixtureSource
$resolvedMySqlSource = Get-NormalizedPath -Path $MySqlSource
if ([string]::IsNullOrWhiteSpace($VcRedistSource)) {
    $VcRedistSource = Join-Path $resolvedLabRoot "downloads\vc_redist.x64.exe"
}
$resolvedVcRedistSource = Get-NormalizedPath -Path $VcRedistSource

$helperHash = Assert-FileHash -Path $resolvedHelperSource -Expected $ExpectedHelperHash -Label "helper"
$fixtureHash = Assert-FileHash -Path $resolvedFixtureSource -Expected $ExpectedFixtureHash -Label "fixture"
if (-not (Test-Path -LiteralPath $resolvedVcRedistSource -PathType Leaf)) {
    throw "Visual C++ runtime installer is missing: $resolvedVcRedistSource"
}
Assert-MicrosoftSignature -Path $resolvedVcRedistSource -Label "Visual C++ runtime installer"
$vcRedistHash = Get-Sha256Hex -Path $resolvedVcRedistSource

foreach ($entry in $AllowedMySqlEntries) {
    $sourceEntry = Join-Path $resolvedMySqlSource $entry
    if (-not (Test-Path -LiteralPath $sourceEntry)) {
        throw "Required MySQL runtime entry is missing: $sourceEntry"
    }
}

$templatePath = Join-Path $PSScriptRoot "AFP-Client-Validation.wsb.template"
$requiredSandboxAssets = @(
    "Initialize-ClientLab.ps1",
    "Start-VirtualTcpSensor.ps1",
    "Export-ClientLabEvidence.ps1",
    "Invoke-HelperNetworkInterruption.ps1",
    "lab-mysql.ini.template"
)
if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
    throw "Sandbox template is missing: $templatePath"
}
foreach ($assetName in $requiredSandboxAssets) {
    $assetPath = Join-Path $PSScriptRoot $assetName
    if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
        throw "Sandbox runtime asset is missing: $assetPath"
    }
}

$inputRoot = Join-Path $resolvedLabRoot "input"
$configRoot = Join-Path $resolvedLabRoot "config"
$resultsRoot = Join-Path $resolvedLabRoot "results"
if (Test-Path -LiteralPath $inputRoot) {
    throw "Input staging directory already exists; refusing to overwrite it: $inputRoot"
}
if (Test-Path -LiteralPath $configRoot) {
    throw "Config staging directory already exists; refusing to overwrite it: $configRoot"
}

$runId = Get-Date -Format "yyyyMMdd-HHmmssfff"
$runResults = Join-Path $resultsRoot $runId
$helperTarget = Join-Path $inputRoot "helper"
$fixtureTarget = Join-Path $inputRoot "fixtures"
$mysqlTarget = Join-Path $inputRoot "mysql"
$scriptsTarget = Join-Path $inputRoot "scripts"
$prerequisiteTarget = Join-Path $inputRoot "prerequisites"
foreach ($directory in @($configRoot, $runResults, $helperTarget, $fixtureTarget, $mysqlTarget, $scriptsTarget, $prerequisiteTarget)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

Copy-Item -LiteralPath $resolvedHelperSource -Destination (Join-Path $helperTarget "AFP_Local_Capture_Helper.exe")
Copy-Item -LiteralPath $resolvedFixtureSource -Destination (Join-Path $fixtureTarget "SIM_PRESSURE_M3232_new_collection.csv")
Copy-Item -LiteralPath $resolvedVcRedistSource -Destination (Join-Path $prerequisiteTarget "vc_redist.x64.exe")
foreach ($entry in $AllowedMySqlEntries) {
    Copy-Item -LiteralPath (Join-Path $resolvedMySqlSource $entry) -Destination $mysqlTarget -Recurse
}
foreach ($assetName in $requiredSandboxAssets) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $assetName) -Destination (Join-Path $scriptsTarget $assetName)
}

$template = Get-Content -LiteralPath $templatePath -Raw
$rendered = $template.Replace("{{INPUT_HOST_FOLDER}}", $inputRoot).Replace("{{RESULTS_HOST_FOLDER}}", $resultsRoot)
$wsbPath = Join-Path $configRoot "AFP-Client-Validation.wsb"
[System.IO.File]::WriteAllText($wsbPath, $rendered, [System.Text.UTF8Encoding]::new($false))

$stagedFiles = @(
    Get-ChildItem -LiteralPath $inputRoot, $configRoot -File -Recurse | ForEach-Object {
        $relative = $_.FullName.Substring($resolvedLabRoot.Length + 1)
        [ordered]@{
            path = $relative
            bytes = $_.Length
            sha256 = Get-Sha256Hex -Path $_.FullName
        }
    }
)
$manifest = [ordered]@{
    schema = "afp-client-lab-stage-v1"
    run_id = $runId
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    lab_root = $resolvedLabRoot
    source_hashes = [ordered]@{
        helper = $helperHash
        fixture = $fixtureHash
        vc_redist = $vcRedistHash
    }
    excluded_names = $ExcludedNames
    files = $stagedFiles
}
$manifestPath = Join-Path $runResults "stage-manifest.json"
$manifestJson = $manifest | ConvertTo-Json -Depth 8
$temporaryManifest = "$manifestPath.tmp"
[System.IO.File]::WriteAllText($temporaryManifest, $manifestJson, [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporaryManifest -Destination $manifestPath

[ordered]@{
    lab_root = $resolvedLabRoot
    run_id = $runId
    wsb = $wsbPath
    manifest = $manifestPath
} | ConvertTo-Json -Depth 4
