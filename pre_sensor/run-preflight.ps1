[CmdletBinding()]
param(
    [ValidateSet("local_direct", "remote_helper")]
    [string]$Mode = "local_direct",
    [string]$Config = "",
    [string]$Output = "",
    [string]$SaveRoot = "",
    [switch]$PromptForMySQL,
    [switch]$RetryPendingMySQL,
    [string]$MySQLHost = "192.168.101.31",
    [ValidateRange(1, 65535)]
    [int]$MySQLPort = 3306,
    [string]$MySQLUser = "afp_app",
    [string]$MySQLDatabase = "afp_state_warning"
)

$ErrorActionPreference = "Stop"
$candidateRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $candidateRoot "AFP_Integrated_System_Modular.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Candidate launcher is missing: $launcher"
}

$launcherArguments = @("--pre-sensor-preflight", "--pre-sensor-mode", $Mode)
if ($Config) {
    $launcherArguments += @("--pre-sensor-config", $Config)
}
if ($Output) {
    $launcherArguments += @("--pre-sensor-output", $Output)
}

$quotedArguments = $launcherArguments | ForEach-Object {
    '"' + ([string]$_).Replace('"', '\"') + '"'
}

$saveVariable = if ($Mode -eq "remote_helper") { "AFP_HELPER_SAVE_ROOT" } else { "AFP_SAVE_ROOT" }
$previousSaveRoot = [Environment]::GetEnvironmentVariable($saveVariable, "Process")
$previousMySQLProfile = [Environment]::GetEnvironmentVariable("AFP_MYSQL_PROFILE", "Process")
if (-not $SaveRoot) {
    $SaveRoot = $previousSaveRoot
}
if (-not $SaveRoot) {
    $candidateDrive = [IO.Path]::GetPathRoot($candidateRoot)
    $SaveRoot = Join-Path $candidateDrive "AFP_Capture"
}
$logVariable = if ($Mode -eq "remote_helper") { "AFP_HELPER_LOG_ROOT" } else { "AFP_LOG_ROOT" }
$logRoot = Join-Path $SaveRoot "pre_sensor_logs"
$manifest = Get-Content -LiteralPath (Join-Path $candidateRoot "RELEASE-MANIFEST.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$helperCapabilities = @{
    protocol_version = [int]$manifest.contracts.helper_protocol_version
    helper_version = [string]$manifest.identity.helper_version
    build_id = [string]$manifest.identity.release_revision
    unified_frame_contract = [string]$manifest.contracts.unified_frame_contract
    configuration_schema_version = [int]$manifest.contracts.configuration_schema_version
    readiness_schema_version = [int]$manifest.contracts.readiness_schema_version
    unified_frame_v1 = $true
    real_capture = $true
    local_csv_save = $true
    features = @("unified_frame_v1", "real_capture", "local_csv_save")
}
$diagnosticPairingProfile = @{
    session_id = "preflight-unpaired"
    capabilities = $helperCapabilities
} | ConvertTo-Json -Depth 6 -Compress
$temporaryDefaults = [ordered]@{
    $saveVariable = $SaveRoot
    $logVariable = $logRoot
    "AFP_INTERFACE_THERMOCOUPLE" = "physical-sensor-pending://thermocouple-8ch"
    "AFP_INTERFACE_PLC" = "physical-sensor-pending://plc-process"
    "AFP_INTERFACE_UVC" = "physical-sensor-pending://uvc-temperature"
    "AFP_INTERFACE_ABB" = "physical-sensor-pending://abb-motion"
    "AFP_INTERFACE_M3232" = "physical-sensor-pending://m3232-pressure"
}
if ($Mode -eq "remote_helper") {
    $temporaryDefaults["AFP_HELPER_SERVER_URL"] = "http://127.0.0.1:8765"
    $temporaryDefaults["AFP_HELPER_PAIRING_PROFILE"] = $diagnosticPairingProfile
}
$previousEnvironment = @{}

$securePassword = $null
$credential = $null
$plainPassword = $null
$mysqlProfile = $null
$exitCode = 1
try {
    foreach ($name in $temporaryDefaults.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($name -eq $saveVariable -or -not $previousEnvironment[$name]) {
            [Environment]::SetEnvironmentVariable(
                $name,
                [string]$temporaryDefaults[$name],
                "Process"
            )
        }
    }
    if ($PromptForMySQL) {
        $securePassword = Read-Host "Target MySQL password for $MySQLUser@$MySQLHost" -AsSecureString
        $credential = [System.Management.Automation.PSCredential]::new($MySQLUser, $securePassword)
        $plainPassword = $credential.GetNetworkCredential().Password
        $mysqlProfile = @{
            enabled = $true
            host = $MySQLHost
            port = $MySQLPort
            user = $MySQLUser
            password = $plainPassword
            database = $MySQLDatabase
            charset = "utf8mb4"
            connect_timeout = 5
        } | ConvertTo-Json -Compress
        [Environment]::SetEnvironmentVariable("AFP_MYSQL_PROFILE", $mysqlProfile, "Process")
    }
    $process = Start-Process -FilePath $launcher -ArgumentList $quotedArguments -Wait -PassThru -WindowStyle Hidden
    $exitCode = $process.ExitCode
    if ($RetryPendingMySQL) {
        $reportDirectory = if ($Output) {
            $Output
        }
        else {
            Join-Path $candidateRoot "verification\pre-sensor"
        }
        $reportPath = Join-Path $reportDirectory "pre-sensor-readiness.json"
        if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            throw "Preflight report is missing; MySQL retry was not attempted: $reportPath"
        }
        $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $mysqlCheck = @($report.checks | Where-Object {
            $_.check_id -eq "environment.target_mysql"
        }) | Select-Object -First 1
        if ($null -ne $mysqlCheck -and $mysqlCheck.status -eq "passed") {
            $retryArguments = @(
                "--pre-sensor-retry-mysql",
                "--pre-sensor-capture-root",
                $SaveRoot
            ) | ForEach-Object {
                '"' + ([string]$_).Replace('"', '\"') + '"'
            }
            $retryProcess = Start-Process -FilePath $launcher -ArgumentList $retryArguments -Wait -PassThru -WindowStyle Hidden
            $exitCode = $retryProcess.ExitCode
            if ($exitCode -eq 0) {
                $finalProcess = Start-Process -FilePath $launcher -ArgumentList $quotedArguments -Wait -PassThru -WindowStyle Hidden
                $exitCode = $finalProcess.ExitCode
            }
        }
    }
}
finally {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $previousEnvironment[$name],
            "Process"
        )
    }
    [Environment]::SetEnvironmentVariable("AFP_MYSQL_PROFILE", $previousMySQLProfile, "Process")
    $plainPassword = $null
    $mysqlProfile = $null
    $credential = $null
    if ($null -ne $securePassword) {
        $securePassword.Dispose()
    }
}
exit $exitCode
