param(
    [int]$Port = 8770,
    [switch]$NoBrowser,
    [switch]$SelfTest
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
    if (-not $NoBrowser) {
        Start-Process $Url
    }
    Write-Host "AFP Interface Monitor and LangChain Diagnosis Demo"
    Write-Host "URL: $Url"
    Write-Host "Press Ctrl+C to stop."
    & $DemoPython -m interface_monitor_demo.server --host 127.0.0.1 --port $Port
}
finally {
    Pop-Location
}
