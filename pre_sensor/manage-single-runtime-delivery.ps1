[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Inventory", "Stage", "Switchover", "StartupPreview", "Evidence", "CleanupPreview", "Cleanup", "Rollback")]
    [string]$Mode,
    [string]$WorkspaceRoot = "",
    [string]$ReferenceRelease = "",
    [string]$StagingPath = "",
    [string]$ApprovedInventoryDigest = "",
    [string]$PublicUrl = "",
    [string]$LanIPv4 = "",
    [string]$FunnelOrigin = "http://127.0.0.1:8770",
    [string]$ExpectedCandidateId = "",
    [string]$GitRef = "HEAD"
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "SingleRuntimePaths.psm1") -Force
Import-Module (Join-Path $PSScriptRoot "SingleRuntimeRelease.psm1") -Force
Import-Module (Join-Path $PSScriptRoot "SingleRuntimeDeployment.psm1") -Force
$configPath = Join-Path $PSScriptRoot "single-runtime-paths.json"
$paths = if ($WorkspaceRoot) {
    Get-AfpDeploymentPaths -ConfigPath $configPath -WorkspaceRootOverride $WorkspaceRoot
} else {
    Get-AfpDeploymentPaths -ConfigPath $configPath
}
$evidenceDir = Join-Path $paths.EvidenceRoot "SINGLE-RUNTIME-MIGRATION"

function Write-State([string]$Name, [psobject]$Value) {
    New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null
    $path = Join-Path $evidenceDir $Name
    $Value | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $path -Encoding UTF8
    return $path
}

if ($Mode -eq "Inventory") {
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    $path = Write-State "inventory.json" $inventory
    [pscustomobject]@{ status = "inventoried"; inventory = $inventory; evidence = $path } | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -in @("Stage", "Rollback")) {
    $readiness = Test-AfpBuildReadiness -Paths $paths -RepoRoot $paths.SourceRoot -GitRef $GitRef -RequiredInputs @(
        (Join-Path $paths.SourceRoot "pre_sensor\release-contract.json"),
        (Join-Path $paths.SourceRoot "modular_runtime\assemble_modular_delivery.ps1")
    ) -RequiredBytes 1
    if ($readiness.status -ne "passed") {
        $readiness | ConvertTo-Json -Depth 10
        exit 2
    }
    $staged = if ($Mode -eq "Rollback") {
        if (-not $ReferenceRelease) { $ReferenceRelease = $paths.RuntimeRoot }
        New-AfpRollbackStage -Paths $paths -GitRef $GitRef -ReferenceRelease $ReferenceRelease
    } else {
        New-AfpStagedRelease -Paths $paths -ReferenceRelease $ReferenceRelease
    }
    $gate = Test-AfpReleaseGate -CandidatePath $staged.staging_path -ExpectedCandidateId $staged.candidate_id
    if ($gate.status -ne "passed") {
        Remove-AfpDeploymentStaging -StagingPath $staged.staging_path -Paths $paths
        $gate | ConvertTo-Json -Depth 10
        exit 3
    }
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    $approval = New-AfpApprovalSummary -Inventory $inventory -Operation Switchover
    $state = [pscustomobject]@{ status = "approval_required"; mode = $Mode.ToLowerInvariant(); git_ref = $GitRef; staged = $staged; gate = $gate; approval = $approval }
    $path = Write-State "stage.json" $state
    [pscustomobject]@{ state = $state; evidence = $path } | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -eq "Switchover") {
    if (-not $ApprovedInventoryDigest -or -not $StagingPath) { throw "Switchover requires ApprovedInventoryDigest and StagingPath." }
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    if ($inventory.digest -ne $ApprovedInventoryDigest) { throw "Runtime inventory changed after approval." }
    $listener = @($inventory.listeners | Where-Object { $_.port -eq 8770 } | Select-Object -First 1)
    $oldProcess = $null
    if ($listener.Count) {
        $oldProcess = @($inventory.processes | Where-Object { $_.pid -eq $listener[0].pid } | Select-Object -First 1)
        if (-not $oldProcess.Count) { throw "The approved 8770 listener is not an inventoried AFP process." }
        Stop-Process -Id $listener[0].pid -Force -ErrorAction Stop
    }
    $promotion = Invoke-AfpRuntimePromotion -StagingPath $StagingPath -Paths $paths -ApprovedInventoryDigest $ApprovedInventoryDigest -CurrentInventory $inventory
    $exe = Join-Path $paths.RuntimeRoot "AFP_Integrated_System_Modular.exe"
    Start-Process -FilePath $exe -WorkingDirectory $paths.RuntimeRoot -WindowStyle Hidden | Out-Null
    $services = Test-AfpRuntimeServices -RuntimeRoot $paths.RuntimeRoot -PublicUrl $PublicUrl -LanIPv4 $LanIPv4 -ExpectedCandidateId $ExpectedCandidateId -FunnelOrigin $FunnelOrigin
    if ($services.status -ne "passed") {
        Get-CimInstance Win32_Process -Filter "Name='AFP_Integrated_System_Modular.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -eq $exe } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Restore-AfpRuntimePromotion -Promotion $promotion -Paths $paths
        if ($oldProcess -and $oldProcess.Count) { Start-Process -FilePath $oldProcess[0].executable_path -WorkingDirectory (Split-Path -Parent $oldProcess[0].executable_path) -WindowStyle Hidden | Out-Null }
        $services | ConvertTo-Json -Depth 10
        exit 4
    }
    Complete-AfpRuntimePromotion -Promotion $promotion -Paths $paths
    [pscustomobject]@{ status = "service_verified"; promotion = $promotion; services = $services } | ConvertTo-Json -Depth 12
    exit 0
}

if ($Mode -eq "StartupPreview") {
    $inventory = Get-AfpRuntimeInventory -PublicUrl $PublicUrl -FunnelOrigin $FunnelOrigin
    New-AfpApprovalSummary -Inventory $inventory -Operation Startup | ConvertTo-Json -Depth 10
    exit 0
}

throw "Mode $Mode requires the evidence/cleanup module implemented by the next task."
