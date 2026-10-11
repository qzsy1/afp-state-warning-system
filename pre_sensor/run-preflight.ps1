[CmdletBinding()]
param(
    [ValidateSet("local_direct", "remote_helper")]
    [string]$Mode = "local_direct",
    [string]$Config = "",
    [string]$Output = "",
    [string]$SaveRoot = "",
    [switch]$PromptForMySQL,
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

$securePassword = $null
$credential = $null
$plainPassword = $null
$mysqlProfile = $null
$exitCode = 1
try {
    [Environment]::SetEnvironmentVariable($saveVariable, $SaveRoot, "Process")
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
}
finally {
    [Environment]::SetEnvironmentVariable($saveVariable, $previousSaveRoot, "Process")
    [Environment]::SetEnvironmentVariable("AFP_MYSQL_PROFILE", $previousMySQLProfile, "Process")
    $plainPassword = $null
    $mysqlProfile = $null
    $credential = $null
    if ($null -ne $securePassword) {
        $securePassword.Dispose()
    }
}
exit $exitCode
