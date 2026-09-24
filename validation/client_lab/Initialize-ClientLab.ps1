[CmdletBinding()]
param(
    [string]$InputRoot = "C:\AFP-Lab\Input",
    [string]$ResultsRoot = "C:\AFP-Lab\Results",
    [string]$WorkRoot = "C:\AFP-Lab\Work",
    [string]$PublicUrl = "https://desktop-410sfvi.tail97fe2c.ts.net/",
    [switch]$DescribeContract
)

$ErrorActionPreference = "Stop"
$AllowedMySqlEntries = @("bin", "lib", "share", "LICENSE")
$VcRuntimePrerequisite = "vc_redist.x64.exe"

if ($DescribeContract) {
    [ordered]@{
        mysql_entries = $AllowedMySqlEntries
        mysql_host = "127.0.0.1"
        mysql_port = 3306
        database = "afp_state_warning"
        credentials_exported = $false
        input_read_only = $true
        vc_runtime_prerequisite = $VcRuntimePrerequisite
        vc_runtime_signature_required = $true
    } | ConvertTo-Json -Depth 4
    return
}

function New-RandomHexSecret {
    param([int]$Bytes = 24)
    $buffer = New-Object byte[] $Bytes
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return ([System.BitConverter]::ToString($buffer)).Replace("-", "")
}

function Wait-TcpPort {
    param([string]$HostName, [int]$Port, [int]$TimeoutSeconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $pending = $client.BeginConnect($HostName, $Port, $null, $null)
            if ($pending.AsyncWaitHandle.WaitOne(500) -and $client.Connected) {
                $client.EndConnect($pending)
                return
            }
        }
        catch { }
        finally { $client.Dispose() }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "MySQL did not listen on 127.0.0.1:$Port within $TimeoutSeconds seconds"
}

function Assert-MicrosoftSignature {
    param([Parameter(Mandatory = $true)][string]$Path)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Visual C++ runtime installer signature is not valid: $($signature.Status)"
    }
    if (-not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
        throw "Visual C++ runtime installer signer is not Microsoft Corporation"
    }
}

foreach ($required in @($InputRoot, $ResultsRoot)) {
    if (-not (Test-Path -LiteralPath $required -PathType Container)) {
        throw "Required Sandbox mapping is missing: $required"
    }
}

$mysqlInput = Join-Path $InputRoot "mysql"
$mysqlRoot = Join-Path $WorkRoot "mysql"
$mysqlData = Join-Path $mysqlRoot "data"
$mysqlIni = Join-Path $mysqlRoot "lab-mysql.ini"
$stateRoot = Join-Path $WorkRoot "state"
$secretRoot = Join-Path $WorkRoot "secrets"
$helperRoot = Join-Path $WorkRoot "helper"
$captureRoot = Join-Path $WorkRoot "captures"
foreach ($directory in @($WorkRoot, $mysqlRoot, $stateRoot, $secretRoot, $helperRoot, $captureRoot)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

if (-not (Test-Path -LiteralPath (Join-Path $mysqlRoot "bin\mysqld.exe") -PathType Leaf)) {
    foreach ($entry in $AllowedMySqlEntries) {
        $source = Join-Path $mysqlInput $entry
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Staged MySQL runtime entry is missing: $source"
        }
        Copy-Item -LiteralPath $source -Destination $mysqlRoot -Recurse
    }
}

$iniTemplatePath = Join-Path $InputRoot "scripts\lab-mysql.ini.template"
$iniTemplate = Get-Content -LiteralPath $iniTemplatePath -Raw
$iniText = $iniTemplate.Replace("{{MYSQL_BASEDIR}}", $mysqlRoot.Replace("\", "/"))
$iniText = $iniText.Replace("{{MYSQL_DATADIR}}", $mysqlData.Replace("\", "/"))
$iniText = $iniText.Replace("{{MYSQL_ERROR_LOG}}", (Join-Path $stateRoot "mysql-error.log").Replace("\", "/"))
[System.IO.File]::WriteAllText($mysqlIni, $iniText, [System.Text.UTF8Encoding]::new($false))

$mysqld = Join-Path $mysqlRoot "bin\mysqld.exe"
$mysql = Join-Path $mysqlRoot "bin\mysql.exe"
$credentialsPath = Join-Path $secretRoot "mysql-client-lab.txt"
$vcRedist = Join-Path $InputRoot "prerequisites\$VcRuntimePrerequisite"
& $mysqld "--version" *> $null
if ($LASTEXITCODE -ne 0) {
    if (-not (Test-Path -LiteralPath $vcRedist -PathType Leaf)) {
        throw "MySQL runtime prerequisite is missing: $vcRedist"
    }
    Assert-MicrosoftSignature -Path $vcRedist
    $install = Start-Process -FilePath $vcRedist -ArgumentList "/install", "/quiet", "/norestart" -Wait -PassThru
    if ($install.ExitCode -notin @(0, 1638, 3010)) {
        throw "Visual C++ runtime installation failed with exit code $($install.ExitCode)"
    }
    & $mysqld "--version" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "MySQL still cannot start after Visual C++ runtime installation (exit code $LASTEXITCODE)"
    }
}
$needsInitialization = -not (Test-Path -LiteralPath (Join-Path $mysqlData "auto.cnf") -PathType Leaf)
if ($needsInitialization) {
    New-Item -ItemType Directory -Force -Path $mysqlData | Out-Null
    $mysqlInitializeOutput = Join-Path $stateRoot "mysql-initialize.stdout.log"
    $mysqlInitializeError = Join-Path $stateRoot "mysql-initialize.stderr.log"
    $initialize = Start-Process -FilePath $mysqld -ArgumentList @(
        "--defaults-file=$mysqlIni", "--initialize-insecure", "--console"
    ) -Wait -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $mysqlInitializeOutput `
        -RedirectStandardError $mysqlInitializeError
    if ($initialize.ExitCode -ne 0) {
        throw "mysqld --initialize-insecure failed with exit code $($initialize.ExitCode); see Sandbox-local initialization logs"
    }
}

$needsAccountInitialization = -not (Test-Path -LiteralPath $credentialsPath -PathType Leaf)
$mysqlBootstrap = Join-Path $secretRoot "mysql-bootstrap.sql"
if ($needsAccountInitialization) {
    $rootSecret = New-RandomHexSecret
    $appSecret = New-RandomHexSecret
    $sql = @"
ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootSecret';
CREATE DATABASE IF NOT EXISTS afp_state_warning CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'afp_app'@'127.0.0.1' IDENTIFIED BY '$appSecret';
ALTER USER 'afp_app'@'127.0.0.1' IDENTIFIED BY '$appSecret';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, DROP ON afp_state_warning.* TO 'afp_app'@'127.0.0.1';
FLUSH PRIVILEGES;
"@
    [System.IO.File]::WriteAllText($mysqlBootstrap, $sql, [System.Text.UTF8Encoding]::new($false))
    & icacls.exe $mysqlBootstrap /inheritance:r /grant:r "$env:USERNAME:(R,W)" | Out-Null
}

$existingServer = Get-Process -Name "mysqld" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($mysqlRoot, [System.StringComparison]::OrdinalIgnoreCase) } |
    Select-Object -First 1
if ($existingServer -and $needsAccountInitialization) {
    throw "MySQL loopback account bootstrap requires the Sandbox-local server to be stopped"
}
if (-not $existingServer) {
    $serverArguments = @("--defaults-file=$mysqlIni", "--console")
    if ($needsAccountInitialization) {
        $serverArguments += "--init-file=$mysqlBootstrap"
    }
    Start-Process -FilePath $mysqld -ArgumentList $serverArguments -WindowStyle Hidden | Out-Null
}
Wait-TcpPort -HostName "127.0.0.1" -Port 3306

if ($needsAccountInitialization) {
    $mysqlVerifyOutput = Join-Path $stateRoot "mysql-account-verify.stdout.log"
    $mysqlVerifyError = Join-Path $stateRoot "mysql-account-verify.stderr.log"
    $verify = Start-Process -FilePath $mysql -ArgumentList @(
        "--protocol=TCP", "--host=127.0.0.1", "--port=3306", "--user=afp_app",
        "--password=$appSecret", '--execute="SELECT 1"'
    ) -Wait -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $mysqlVerifyOutput `
        -RedirectStandardError $mysqlVerifyError
    if ($verify.ExitCode -ne 0) {
        throw "MySQL loopback application account verification failed with exit code $($verify.ExitCode)"
    }
    $credentialText = @"
AFP client lab MySQL (Sandbox only)
Host: 127.0.0.1
Port: 3306
Database: afp_state_warning
User: afp_app
Password: $appSecret

This credential is generated inside Windows Sandbox and is not exported to F drive.
"@
    [System.IO.File]::WriteAllText($credentialsPath, $credentialText, [System.Text.UTF8Encoding]::new($false))
    & icacls.exe $credentialsPath /inheritance:r /grant:r "$env:USERNAME:(R,W)" | Out-Null
    Remove-Item -LiteralPath $mysqlBootstrap -Force
}

$helperSource = Join-Path $InputRoot "helper\AFP_Local_Capture_Helper.exe"
$helperTarget = Join-Path $helperRoot "AFP_Local_Capture_Helper.exe"
if (-not (Test-Path -LiteralPath $helperTarget -PathType Leaf)) {
    Copy-Item -LiteralPath $helperSource -Destination $helperTarget
}

$safeStatus = [ordered]@{
    schema = "afp-client-lab-environment-v1"
    initialized_at = (Get-Date).ToUniversalTime().ToString("o")
    mysql_host = "127.0.0.1"
    mysql_port = 3306
    mysql_database = "afp_state_warning"
    mysql_user = "afp_app"
    credentials_exported = $false
    helper_file = [System.IO.Path]::GetFileName($helperTarget)
}
$safeStatusPath = Join-Path $ResultsRoot "sandbox-initialization.json"
[System.IO.File]::WriteAllText($safeStatusPath, ($safeStatus | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))

$exporter = Join-Path $InputRoot "scripts\Export-ClientLabEvidence.ps1"
$existingExporter = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "powershell.exe" -and $_.CommandLine -like "*$exporter*" } |
    Select-Object -First 1
if (-not $existingExporter) {
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $exporter,
        "-ResultsRoot", $ResultsRoot,
        "-FeederEvidencePath", (Join-Path $stateRoot "feeder-status.json"),
        "-MySqlCountPath", (Join-Path $stateRoot "mysql-row-count.json"),
        "-CsvRoot", $captureRoot
    ) -WindowStyle Hidden | Out-Null
}

$existingHelper = Get-Process -Name "AFP_Local_Capture_Helper" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.Equals($helperTarget, [System.StringComparison]::OrdinalIgnoreCase) } |
    Select-Object -First 1
if (-not $existingHelper) {
    Start-Process -FilePath $helperTarget | Out-Null
}

$viewerScript = "Clear-Host; Get-Content -LiteralPath '$credentialsPath'; Write-Host ''; Write-Host 'Keep this Sandbox window open. These credentials exist only inside the Sandbox.'"
$viewerEncoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($viewerScript))
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-NoExit", "-EncodedCommand", $viewerEncoded | Out-Null

$edgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path -LiteralPath $edgePath -PathType Leaf)) {
    throw "Microsoft Edge is missing inside Windows Sandbox: $edgePath"
}
Start-Process -FilePath $edgePath -ArgumentList $PublicUrl | Out-Null

Write-Output "AFP client lab initialized. Credentials remain only at $credentialsPath inside Sandbox."
