[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 19001,
    [ValidateRange(0.1, 1000.0)][double]$RateHz = 50.0,
    [ValidateRange(1, 10000000)][int]$TotalRows = 30000,
    [string]$EvidencePath = "C:\AFP-Lab\Work\state\feeder-status.json"
)

$ErrorActionPreference = "Stop"

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $temporary = "$Path.tmp"
    $json = $Value | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($temporary, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function New-SyntheticChannels {
    param([Parameter(Mandatory = $true)][int]$Sequence)
    $phase = $Sequence / 25.0
    return [ordered]@{
        "温度" = [Math]::Round(320.0 + 2.0 * [Math]::Sin($phase), 6)
        "压力" = [Math]::Round(500.0 + 5.0 * [Math]::Cos($phase), 6)
        "薄膜压力" = [Math]::Round(0.45 + 0.02 * [Math]::Sin($phase / 2.0), 6)
        "ROI平均温度" = [Math]::Round(318.0 + 1.5 * [Math]::Sin($phase), 6)
        "张力" = [Math]::Round(42.0 + 0.5 * [Math]::Cos($phase), 6)
        "线速度" = [Math]::Round(80.0 + 0.2 * [Math]::Sin($phase), 6)
        "ABB_X" = [Math]::Round(100.0 + 0.1 * $Sequence, 6)
        "ABB_Y" = [Math]::Round(200.0 + 0.05 * $Sequence, 6)
        "ABB_Z" = [Math]::Round(300.0 + 0.02 * $Sequence, 6)
        "温度1" = [Math]::Round(315.0 + 0.1 * [Math]::Sin($phase), 6)
        "温度2" = [Math]::Round(316.0 + 0.1 * [Math]::Sin($phase), 6)
        "温度3" = [Math]::Round(317.0 + 0.1 * [Math]::Sin($phase), 6)
        "温度4" = [Math]::Round(318.0 + 0.1 * [Math]::Sin($phase), 6)
        "温度5" = [Math]::Round(319.0 + 0.1 * [Math]::Sin($phase), 6)
        "温度6" = [Math]::Round(320.0 + 0.1 * [Math]::Sin($phase), 6)
        "温度7" = [Math]::Round(321.0 + 0.1 * [Math]::Sin($phase), 6)
        "温度8" = [Math]::Round(322.0 + 0.1 * [Math]::Sin($phase), 6)
    }
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
$client = $null
$writer = $null
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$startedAt = (Get-Date).ToUniversalTime().ToString("o")

try {
    $listener.Start(1)
    Write-AtomicJson -Path $EvidencePath -Value ([ordered]@{
        schema = "afp-client-lab-feeder-v1"
        synthetic_client_lab = $true
        listening = $true
        endpoint = "127.0.0.1:$Port"
        rate_hz = $RateHz
        target_rows = $TotalRows
        produced_rows = 0
        last_sequence = 0
        started_at = $startedAt
        elapsed_seconds = 0.0
    })
    $client = $listener.AcceptTcpClient()
    $writer = [System.IO.StreamWriter]::new($client.GetStream(), [System.Text.UTF8Encoding]::new($false))
    $writer.AutoFlush = $true

    for ($sequence = 1; $sequence -le $TotalRows; $sequence++) {
        $dueSeconds = $sequence / $RateHz
        while ($clock.Elapsed.TotalSeconds -lt $dueSeconds) {
            $remainingMs = [Math]::Floor(($dueSeconds - $clock.Elapsed.TotalSeconds) * 1000.0)
            if ($remainingMs -gt 2) {
                Start-Sleep -Milliseconds ([Math]::Min(10, $remainingMs - 1))
            }
        }
        $row = [ordered]@{
            sequence = $sequence
            generated_monotonic_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 6)
            synthetic_client_lab = $true
            channels = New-SyntheticChannels -Sequence $sequence
        }
        $writer.WriteLine(($row | ConvertTo-Json -Depth 5 -Compress))

        if (($sequence % [Math]::Max(1, [int][Math]::Round($RateHz))) -eq 0 -or $sequence -eq $TotalRows) {
            Write-AtomicJson -Path $EvidencePath -Value ([ordered]@{
                schema = "afp-client-lab-feeder-v1"
                synthetic_client_lab = $true
                listening = $true
                endpoint = "127.0.0.1:$Port"
                rate_hz = $RateHz
                target_rows = $TotalRows
                produced_rows = $sequence
                last_sequence = $sequence
                started_at = $startedAt
                elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 6)
            })
        }
    }
}
finally {
    $clock.Stop()
    if ($writer) { $writer.Dispose() }
    if ($client) { $client.Dispose() }
    $listener.Stop()
    Write-AtomicJson -Path $EvidencePath -Value ([ordered]@{
        schema = "afp-client-lab-feeder-v1"
        synthetic_client_lab = $true
        listening = $false
        endpoint = "127.0.0.1:$Port"
        rate_hz = $RateHz
        target_rows = $TotalRows
        produced_rows = [Math]::Min($TotalRows, [Math]::Floor($clock.Elapsed.TotalSeconds * $RateHz))
        last_sequence = [Math]::Min($TotalRows, [Math]::Floor($clock.Elapsed.TotalSeconds * $RateHz))
        started_at = $startedAt
        elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 6)
    })
}
