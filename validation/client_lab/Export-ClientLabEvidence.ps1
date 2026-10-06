[CmdletBinding()]
param(
    [string]$ResultsRoot = "C:\AFP-Lab\Results",
    [string]$FeederEvidencePath = "C:\AFP-Lab\Work\state\feeder-status.json",
    [string]$MySqlCountPath = "C:\AFP-Lab\Work\state\mysql-row-count.json",
    [string]$CsvRoot = "C:\AFP-Lab\Work\captures",
    [ValidateRange(1, 3600)][int]$IntervalSeconds = 60,
    [switch]$Once
)

$ErrorActionPreference = "Stop"

function Read-JsonValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        $value = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        return $value.$Name
    }
    catch {
        return $null
    }
}

function Get-LatestCsvRowCount {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return $null }
    $latest = Get-ChildItem -LiteralPath $Root -Filter "*.csv" -File -Recurse |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if (-not $latest) { return $null }
    $reader = [System.IO.File]::OpenText($latest.FullName)
    $lines = 0
    try {
        while ($null -ne $reader.ReadLine()) { $lines++ }
    }
    finally {
        $reader.Dispose()
    }
    return [Math]::Max(0, $lines - 1)
}

function Write-AtomicJsonLine {
    param([string]$Path, $Value)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $existing = if (Test-Path -LiteralPath $Path -PathType Leaf) {
        [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    } else { "" }
    $line = ($Value | ConvertTo-Json -Depth 5 -Compress) + [Environment]::NewLine
    $temporary = "$Path.tmp"
    [System.IO.File]::WriteAllText($temporary, $existing + $line, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

$outputPath = Join-Path $ResultsRoot "minute-metrics.jsonl"
do {
    $helper = Get-Process -Name "AFP_Local_Capture_Helper" -ErrorAction SilentlyContinue | Select-Object -First 1
    $record = [ordered]@{
        schema = "afp-client-lab-minute-metric-v1"
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        helper_running = [bool]$helper
        helper_process_id = if ($helper) { $helper.Id } else { $null }
        feeder_produced_count = Read-JsonValue -Path $FeederEvidencePath -Name "produced_rows"
        mysql_row_count = Read-JsonValue -Path $MySqlCountPath -Name "row_count"
        csv_row_count = Get-LatestCsvRowCount -Root $CsvRoot
        network_available = [System.Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable()
    }
    Write-AtomicJsonLine -Path $outputPath -Value $record
    if (-not $Once) { Start-Sleep -Seconds $IntervalSeconds }
} while (-not $Once)

Write-Output $outputPath
