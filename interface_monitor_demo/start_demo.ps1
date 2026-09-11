param(
    [int]$Port = 8770,
    [switch]$NoBrowser,
    [switch]$SelfTest,
    [switch]$ReadyTest
)

$ErrorActionPreference = "Stop"
$DemoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $DemoDir
$VenvDir = Join-Path $DemoDir ".venv"
$DemoPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $DemoDir "requirements.txt"

if (-not (Test-Path -LiteralPath $DemoPython -PathType Leaf)) {
    Write-Host "Creating the isolated Python 3.11 environment..."
    & py -3.11 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Demo environment. Install Python 3.11 first."
    }
}

& $DemoPython -c "import langchain_core; assert langchain_core.__version__ == '1.6.2'" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing pinned Demo dependencies..."
    & $DemoPython -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Demo dependency installation failed."
    }
}

Push-Location $RepoRoot
try {
    if ($SelfTest) {
        & $DemoPython -c "from interface_monitor_demo.server import create_server; server=create_server(port=0); print('SELF_TEST_OK', server.server_address[1]); server.server_close()"
        if ($LASTEXITCODE -ne 0) {
            throw "Demo self-test failed."
        }
        exit 0
    }

    $Url = "http://127.0.0.1:$Port"
    Write-Host "AFP Interface Monitor and LangChain Diagnosis Demo"
    Write-Host "URL: $Url"
    Write-Host "Press Ctrl+C to stop."
    $ServerProcess = $null
    try {
        $ServerProcess = Start-Process -FilePath $DemoPython `
            -ArgumentList @("-m", "interface_monitor_demo.server", "--host", "127.0.0.1", "--port", "$Port") `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru

        $Ready = $false
        for ($Attempt = 0; $Attempt -lt 100; $Attempt += 1) {
            if ($ServerProcess.HasExited) {
                throw "Demo server exited before becoming ready."
            }
            try {
                $Status = Invoke-RestMethod -Uri "$Url/api/bootstrap" -TimeoutSec 1
                if ($Status.application.version -eq "demo-1.0") {
                    $Ready = $true
                    break
                }
            }
            catch {
                Start-Sleep -Milliseconds 100
            }
        }
        if (-not $Ready) {
            throw "Demo server did not become ready within 10 seconds."
        }
        if ($ReadyTest) {
            Write-Host "READY_TEST_OK $Url"
            return
        }
        if (-not $NoBrowser) {
            Start-Process $Url
        }
        Wait-Process -Id $ServerProcess.Id
    }
    finally {
        if ($null -ne $ServerProcess -and -not $ServerProcess.HasExited) {
            Stop-Process -Id $ServerProcess.Id -Force
        }
    }
}
finally {
    Pop-Location
}
