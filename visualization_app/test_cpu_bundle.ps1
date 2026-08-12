$ErrorActionPreference = "Continue"
$bundle = "C:\Users\xlq\AppData\Local\Temp\AFPBuild\dist\AFP_State_Warning_System"
$port = 8951
Get-Process AFP_State_Warning_System -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Process -FilePath "$bundle\AFP_State_Warning_System.exe" -ArgumentList "--server","--no-browser","--port",$port -WorkingDirectory $bundle | Out-Null
$ok = $false
for ($i = 1; $i -le 90; $i++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/api/health" -TimeoutSec 2
        Write-Output $response.Content
        $ok = $true
        break
    } catch {}
}
if (-not $ok) { Write-Error "bundle health check failed"; exit 1 }
Get-Process AFP_State_Warning_System -ErrorAction SilentlyContinue | Stop-Process -Force
