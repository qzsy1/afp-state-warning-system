Set-StrictMode -Version Latest

$pathsModule = Join-Path $PSScriptRoot "SingleRuntimePaths.psm1"
Import-Module $pathsModule

function Get-AfpTextSha256 {
    param([Parameter(Mandatory = $true)][string]$Text)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Get-AfpBytesSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function ConvertTo-AfpPublicEndpoint {
    param([string]$Value)
    if (-not $Value) { return "" }
    try {
        $uri = [uri]$Value
        $port = if ($uri.IsDefaultPort) { "" } else { ":$($uri.Port)" }
        return "$($uri.Scheme)://$($uri.Host)$port"
    } catch {
        return "invalid"
    }
}

function Get-AfpStartupTarget {
    param([psobject]$Record)
    if ($Record.PSObject.Properties.Name -contains "TargetPath" -and $Record.TargetPath) {
        return [string]$Record.TargetPath
    }
    $value = if ($Record.PSObject.Properties.Name -contains "Value") { [string]$Record.Value } else { "" }
    $matches = [regex]::Matches($value, '(?i)(?:"([^\"]+\.(?:exe|ps1))"|([^\s]+\.(?:exe|ps1)))')
    $targets = @($matches | ForEach-Object {
        if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value }
    })
    $scriptTarget = @($targets | Where-Object { $_ -match '(?i)\.ps1$' } | Select-Object -Last 1)
    if ($scriptTarget.Count) { return [string]$scriptTarget[0] }
    if ($targets.Count) { return [string]$targets[0] }
    return ""
}

function Get-AfpRuntimeInventory {
    [CmdletBinding()]
    param(
        [object[]]$ProcessRecords,
        [object[]]$ListenerRecords,
        [object[]]$StartupRecords,
        [string]$PublicUrl = "",
        [string]$FunnelOrigin = ""
    )

    if (-not $PSBoundParameters.ContainsKey("ProcessRecords")) {
        $ProcessRecords = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq "AFP_Integrated_System_Modular.exe" })
    }
    if (-not $PSBoundParameters.ContainsKey("ListenerRecords")) {
        $ListenerRecords = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in @(8770, 8771) })
    }
    if (-not $PSBoundParameters.ContainsKey("StartupRecords")) {
        $StartupRecords = @()
        $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        $run = Get-ItemProperty -LiteralPath $runKey -ErrorAction SilentlyContinue
        if ($run) {
            foreach ($property in $run.PSObject.Properties | Where-Object { $_.Name -like "AFP*" }) {
                $StartupRecords += [pscustomobject]@{ Name = $property.Name; Value = [string]$property.Value; Source = "HKCU Run" }
            }
        }
        foreach ($task in @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "AFP*" })) {
            foreach ($action in @($task.Actions)) {
                $StartupRecords += [pscustomobject]@{
                    Name = $task.TaskName
                    Value = (([string]$action.Execute) + " " + ([string]$action.Arguments)).Trim()
                    TargetPath = [string]$action.Execute
                    Source = "ScheduledTask"
                }
            }
        }
    }

    $processes = @($ProcessRecords | ForEach-Object {
        $path = [string]$_.ExecutablePath
        $commandLine = if ($_.PSObject.Properties.Name -contains "CommandLine") { [string]$_.CommandLine } else { "" }
        $identity = "unknown"
        if ($path) {
            $processRoot = Split-Path -Parent $path
            $manifestPath = Join-Path $processRoot "RELEASE-MANIFEST.json"
            $versionPath = Join-Path $processRoot "VERSION.json"
            if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
                try {
                    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    $identity = [string]$manifest.candidate.candidate_id
                } catch { $identity = "unknown" }
            } elseif (Test-Path -LiteralPath $versionPath -PathType Leaf) {
                try {
                    $version = Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    $identity = [string]$version.application_version
                } catch { $identity = "unknown" }
            }
        }
        [pscustomobject][ordered]@{
            pid = [int]$_.ProcessId
            name = [string]$_.Name
            executable_path = $path
            release_identity = $identity
            launch_arguments = if ($commandLine -match '(?i)(?:^|\s)--server-only(?:\s|$)') { "--server-only" } else { "" }
        }
    } | Sort-Object pid)
    $listeners = @($ListenerRecords | ForEach-Object {
        [pscustomobject][ordered]@{
            port = [int]$_.LocalPort
            address = [string]$_.LocalAddress
            pid = [int]$_.OwningProcess
        }
    } | Sort-Object port, pid)
    $startup = @($StartupRecords | ForEach-Object {
        $raw = ""
        if ($_.PSObject.Properties.Name -contains "Value") { $raw = [string]$_.Value }
        $hash = ""
        if ($_.PSObject.Properties.Name -contains "ValueHash" -and $_.ValueHash) {
            $hash = [string]$_.ValueHash
        } else {
            $hash = Get-AfpTextSha256 -Text $raw
        }
        [pscustomobject][ordered]@{
            name = [string]$_.Name
            source = [string]$_.Source
            target_path = (Get-AfpStartupTarget -Record $_)
            value_sha256 = $hash
            has_sensitive_arguments = [bool]($raw -match '(?i)(password|token|pair(?:ing)?[_-]?code|credential|secret)')
        }
    } | Sort-Object source, name)

    $stable = [ordered]@{
        processes = $processes
        listeners = $listeners
        startup = $startup
        public_url = ConvertTo-AfpPublicEndpoint -Value $PublicUrl
        funnel_origin = ConvertTo-AfpPublicEndpoint -Value $FunnelOrigin
    }
    $digest = Get-AfpTextSha256 -Text ($stable | ConvertTo-Json -Depth 10 -Compress)
    return [pscustomobject][ordered]@{
        captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        processes = $processes
        listeners = $listeners
        startup = $startup
        public_url = $stable.public_url
        funnel_origin = $stable.funnel_origin
        digest = $digest
    }
}

function Stop-AfpManagedRuntimeProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Inventory,
        [Parameter(Mandatory = $true)][string]$RuntimeExecutable,
        [scriptblock]$StopRunner
    )

    $resolvedExecutable = [IO.Path]::GetFullPath($RuntimeExecutable)
    $processes = @($Inventory.processes | Where-Object {
        $_.executable_path -and
        [string]::Equals(
            [IO.Path]::GetFullPath([string]$_.executable_path),
            $resolvedExecutable,
            [StringComparison]::OrdinalIgnoreCase
        )
    } | Sort-Object pid)
    foreach ($process in $processes) {
        if ($StopRunner) {
            & $StopRunner ([int]$process.pid) | Out-Null
        } else {
            Stop-Process -Id ([int]$process.pid) -Force -ErrorAction Stop
            Wait-Process -Id ([int]$process.pid) -Timeout 15 -ErrorAction SilentlyContinue
        }
    }
    return $processes
}

function Start-AfpManagedRuntimeProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Processes,
        [Parameter(Mandatory = $true)][string]$RuntimeExecutable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [scriptblock]$StartRunner
    )

    $started = 0
    $orderedProcesses = @($Processes | Sort-Object `
        @{ Expression = { if ([string]$_.launch_arguments -eq "--server-only") { 0 } else { 1 } } }, `
        @{ Expression = { [int]$_.pid } })
    foreach ($process in $orderedProcesses) {
        $arguments = if ($process.PSObject.Properties.Name -contains "launch_arguments") { [string]$process.launch_arguments } else { "" }
        if ($StartRunner) {
            & $StartRunner $RuntimeExecutable $arguments $WorkingDirectory | Out-Null
        } else {
            $start = @{
                FilePath = $RuntimeExecutable
                WorkingDirectory = $WorkingDirectory
                WindowStyle = "Hidden"
            }
            if ($arguments) { $start.ArgumentList = $arguments }
            Start-Process @start | Out-Null
        }
        $started++
    }
    return $started
}

function Test-AfpInventoryPathWithin {
    param([string]$Path, [string]$Root)
    if (-not $Path -or -not $Root) { return $false }
    try {
        $candidate = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        $parent = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        return $candidate.Equals($parent, [StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($parent + '\', [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Get-AfpMigrationInventory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)][psobject]$RuntimeInventory,
        [object[]]$DirectoryTargets,
        [string[]]$ExternalTargets = @(),
        [scriptblock]$FreeSpaceProvider,
        [scriptblock]$RollbackProbe
    )

    if (-not $PSBoundParameters.ContainsKey("DirectoryTargets")) {
        $discovered = [Collections.Generic.List[object]]::new()
        foreach ($entry in @(
            [pscustomobject]@{ path = $Paths.RuntimeRoot; kind = "fixed_runtime" },
            [pscustomobject]@{ path = $Paths.EvidenceRoot; kind = "fixed_evidence" },
            [pscustomobject]@{ path = $Paths.StagingRoot; kind = "staging" }
        )) { $discovered.Add($entry) }
        $candidateRoot = Join-Path $Paths.SourceRoot "artifacts\pre-sensor-candidates"
        foreach ($item in @(Get-ChildItem -LiteralPath $candidateRoot -Directory -ErrorAction SilentlyContinue)) {
            $discovered.Add([pscustomobject]@{ path = $item.FullName; kind = "historical_candidate" })
        }
        $deliveryRoot = Join-Path $Paths.SourceRoot "delivery"
        foreach ($item in @(Get-ChildItem -LiteralPath $deliveryRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "AFP_Integrated_System_Modular*" })) {
            $discovered.Add([pscustomobject]@{ path = $item.FullName; kind = "historical_delivery" })
        }
        foreach ($target in $ExternalTargets) {
            $kind = if ($target -like "*Evidence*") { "legacy_evidence" } else { "legacy_candidate" }
            $discovered.Add([pscustomobject]@{ path = $target; kind = $kind })
        }
        foreach ($process in @($RuntimeInventory.processes)) {
            if ($process.executable_path) {
                $discovered.Add([pscustomobject]@{ path = (Split-Path -Parent $process.executable_path); kind = "active_runtime_source" })
            }
        }
        foreach ($startup in @($RuntimeInventory.startup)) {
            if ($startup.target_path) {
                $discovered.Add([pscustomobject]@{ path = (Split-Path -Parent $startup.target_path); kind = "startup_source" })
            }
        }
        $DirectoryTargets = @($discovered)
    }

    $directories = [Collections.Generic.List[object]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($target in @($DirectoryTargets)) {
        $rawPath = if ($target -is [string]) { [string]$target } else { [string]$target.path }
        $kind = if ($target -is [string]) { "historical" } else { [string]$target.kind }
        if (-not $rawPath) { continue }
        $resolved = [IO.Path]::GetFullPath($rawPath).TrimEnd('\')
        if (-not $seen.Add($resolved)) { continue }
        $exists = Test-Path -LiteralPath $resolved -PathType Container
        $size = 0L
        $candidateId = ""
        $applicationVersion = ""
        if ($exists) {
            foreach ($file in @(Get-ChildItem -LiteralPath $resolved -Recurse -File -Force -ErrorAction SilentlyContinue)) {
                $size += [long]$file.Length
            }
            $manifestPath = Join-Path $resolved "RELEASE-MANIFEST.json"
            $versionPath = Join-Path $resolved "VERSION.json"
            if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
                try { $candidateId = [string](Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).candidate.candidate_id } catch {}
            }
            if (Test-Path -LiteralPath $versionPath -PathType Leaf) {
                try { $applicationVersion = [string](Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8 | ConvertFrom-Json).application_version } catch {}
            }
        }
        $processPids = @($RuntimeInventory.processes | Where-Object { Test-AfpInventoryPathWithin -Path $_.executable_path -Root $resolved } | ForEach-Object { [int]$_.pid })
        $startupNames = @($RuntimeInventory.startup | Where-Object { Test-AfpInventoryPathWithin -Path $_.target_path -Root $resolved } | ForEach-Object { [string]$_.name })
        $directories.Add([pscustomobject][ordered]@{
            path = $resolved
            kind = $kind
            exists = $exists
            size_bytes = $size
            candidate_id = $candidateId
            application_version = $applicationVersion
            active_process_pids = $processPids
            startup_references = $startupNames
        })
    }

    $freeBytes = -1L
    try {
        if ($FreeSpaceProvider) { $freeBytes = [long](& $FreeSpaceProvider $Paths.WorkspaceRoot) }
        else {
            $driveName = ([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Paths.WorkspaceRoot))).TrimEnd('\').TrimEnd(':')
            $freeBytes = [long](Get-PSDrive -Name $driveName -ErrorAction Stop).Free
        }
    } catch {}

    if ($RollbackProbe) {
        $rollback = & $RollbackProbe $Paths.SourceRoot
    } else {
        $revisionOutput = @(& git -C $Paths.SourceRoot rev-parse HEAD 2>$null)
        $revisionOk = $LASTEXITCODE -eq 0 -and $revisionOutput.Count -gt 0
        $statusOutput = @(& git -C $Paths.SourceRoot status --porcelain 2>$null)
        $statusOk = $LASTEXITCODE -eq 0
        & git -C $Paths.SourceRoot lfs env *> $null
        $lfsOk = $LASTEXITCODE -eq 0
        $inputsOk = (Test-Path -LiteralPath (Join-Path $Paths.SourceRoot "pre_sensor\release-contract.json") -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $Paths.SourceRoot "modular_runtime\assemble_modular_delivery.ps1") -PathType Leaf)
        $rollback = [pscustomobject]@{
            source_revision = if ($revisionOk) { [string]$revisionOutput[0] } else { "" }
            working_tree_clean = $statusOk -and $statusOutput.Count -eq 0
            git_lfs_available = $lfsOk
            required_inputs_available = $inputsOk
        }
    }
    $rollbackReady = [bool]$rollback.source_revision -and [bool]$rollback.working_tree_clean -and [bool]$rollback.git_lfs_available -and [bool]$rollback.required_inputs_available
    return [pscustomobject][ordered]@{
        captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        status = "inventoried"
        runtime = $RuntimeInventory
        directories = @($directories)
        disk = [pscustomobject]@{ workspace_root = $Paths.WorkspaceRoot; free_bytes = $freeBytes }
        rollback = [pscustomobject]@{
            ready = $rollbackReady
            source_revision = [string]$rollback.source_revision
            working_tree_clean = [bool]$rollback.working_tree_clean
            git_lfs_available = [bool]$rollback.git_lfs_available
            required_inputs_available = [bool]$rollback.required_inputs_available
        }
    }
}

function Test-AfpInventoryUnchanged {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Expected,
        [Parameter(Mandatory = $true)][psobject]$Current
    )
    return [string]::Equals([string]$Expected.digest, [string]$Current.digest, [StringComparison]::OrdinalIgnoreCase)
}

function New-AfpApprovalSummary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Inventory,
        [Parameter(Mandatory = $true)][ValidateSet("Switchover", "Startup", "Cleanup")][string]$Operation
    )
    $listener = @($Inventory.listeners | Where-Object { $_.port -eq 8770 } | Select-Object -First 1)
    $pidValue = if ($listener.Count) { [int]$listener[0].pid } else { 0 }
    $process = @($Inventory.processes | Where-Object { $_.pid -eq $pidValue } | Select-Object -First 1)
    $path = if ($process.Count) { [string]$process[0].executable_path } else { "unknown" }
    return [pscustomobject][ordered]@{
        operation = $Operation.ToLowerInvariant()
        current_listener_pid = $pidValue
        current_listener_path = $path
        current_release_identity = if ($process.Count) { [string]$process[0].release_identity } else { "unknown" }
        public_url = [string]$Inventory.public_url
        funnel_origin = [string]$Inventory.funnel_origin
        impact = if ($Operation -eq "Switchover") { "8770/8771 will be briefly unavailable" } else { "Only the listed AFP targets will change" }
        expected_interruption = if ($Operation -eq "Switchover") { "brief AFP web interruption" } else { "none expected" }
        rollback_plan = if ($path -ne "unknown") { "restart the previously inventoried AFP executable" } else { "rebuild the recorded Git revision into the fixed runtime" }
        inventory_digest = [string]$Inventory.digest
    }
}

function Invoke-AfpRuntimePromotion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StagingPath,
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)][string]$ApprovedInventoryDigest,
        [psobject]$CurrentInventory
    )
    if (-not $CurrentInventory) { $CurrentInventory = Get-AfpRuntimeInventory }
    if (-not [string]::Equals($ApprovedInventoryDigest, [string]$CurrentInventory.digest, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Runtime inventory changed after approval."
    }
    $staging = Assert-AfpMutableTarget -Path $StagingPath -Paths $Paths -Kind Staging
    $runtime = Assert-AfpMutableTarget -Path $Paths.RuntimeRoot -Paths $Paths -Kind Runtime
    if (-not (Test-Path -LiteralPath $staging -PathType Container)) { throw "Staged release is missing: $staging" }
    $swap = Join-Path $Paths.WorkspaceRoot (".afp-runtime-swap-" + [guid]::NewGuid().ToString("N"))
    $hadPrevious = Test-Path -LiteralPath $runtime -PathType Container
    try {
        if ($hadPrevious) { Move-Item -LiteralPath $runtime -Destination $swap }
        Move-Item -LiteralPath $staging -Destination $runtime
    } catch {
        if ((Test-Path -LiteralPath $swap -PathType Container) -and -not (Test-Path -LiteralPath $runtime)) {
            Move-Item -LiteralPath $swap -Destination $runtime
        }
        throw
    }
    return [pscustomobject][ordered]@{
        status = "switched"
        runtime_path = $runtime
        swap_path = if ($hadPrevious) { $swap } else { "" }
        original_staging_path = $staging
        had_previous_runtime = $hadPrevious
        inventory_digest = [string]$CurrentInventory.digest
    }
}

function Restore-AfpRuntimePromotion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Promotion,
        [Parameter(Mandatory = $true)][psobject]$Paths
    )
    $runtime = Assert-AfpMutableTarget -Path $Promotion.runtime_path -Paths $Paths -Kind Runtime
    $staging = Assert-AfpMutableTarget -Path $Promotion.original_staging_path -Paths $Paths -Kind Staging
    if (Test-Path -LiteralPath $staging) { throw "Cannot restore while the original staging path exists." }
    if (Test-Path -LiteralPath $runtime -PathType Container) { Move-Item -LiteralPath $runtime -Destination $staging }
    if ($Promotion.had_previous_runtime) {
        if (-not (Test-Path -LiteralPath $Promotion.swap_path -PathType Container)) { throw "Runtime rollback material is missing." }
        Move-Item -LiteralPath $Promotion.swap_path -Destination $runtime
    }
}

function Complete-AfpRuntimePromotion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Promotion,
        [Parameter(Mandatory = $true)][psobject]$Paths
    )
    if ($Promotion.swap_path) {
        $swap = Assert-AfpMutableTarget -Path $Promotion.swap_path -Paths $Paths -Kind Cleanup
        if (-not (Split-Path -Leaf $swap).StartsWith(".afp-runtime-swap-", [StringComparison]::OrdinalIgnoreCase)) {
            throw "Runtime swap cleanup target is invalid."
        }
        if (Test-Path -LiteralPath $swap) { Remove-Item -LiteralPath $swap -Recurse -Force }
    }
}

function Test-AfpPublicStaticIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$PublicUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedCandidateId,
        [scriptblock]$ContentFetcher
    )

    $endpoint = ConvertTo-AfpPublicEndpoint -Value $PublicUrl
    $assetPath = Join-Path $RuntimeRoot "app\ui\app.js"
    $manifestPath = Join-Path $RuntimeRoot "RELEASE-MANIFEST.json"
    $uri = ""
    try {
        if ($endpoint -eq "invalid" -or -not $endpoint) { throw "public endpoint is invalid" }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$manifest.candidate.candidate_id -ne $ExpectedCandidateId) {
            throw "runtime candidate identity mismatch"
        }
        $localBytes = [IO.File]::ReadAllBytes($assetPath)
        $localHash = Get-AfpBytesSha256 -Bytes $localBytes
        $uri = "$endpoint/app.js?afp_identity_check=$($localHash.Substring(0, 16))"
        if ($ContentFetcher) {
            $remoteBytes = [byte[]](& $ContentFetcher $uri)
        } else {
            $client = [Net.WebClient]::new()
            try { $remoteBytes = [byte[]]$client.DownloadData($uri) }
            finally { $client.Dispose() }
        }
        $remoteHash = Get-AfpBytesSha256 -Bytes $remoteBytes
        $passed = $localHash -eq $remoteHash
        return [pscustomobject]@{
            passed = $passed
            detail = if ($passed) { "public app.js matches fixed runtime" } else { "public app.js digest mismatch" }
            uri = $uri
        }
    } catch {
        return [pscustomobject]@{ passed = $false; detail = "public static identity probe failed"; uri = $uri }
    }
}

function Test-AfpRuntimeServices {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [string]$PublicUrl = "",
        [string]$LanIPv4 = "",
        [string]$ExpectedCandidateId = "",
        [string]$FunnelOrigin = "",
        [scriptblock]$ProbeRunner
    )
    $targets = [ordered]@{
        listener_8770 = Join-Path $RuntimeRoot "AFP_Integrated_System_Modular.exe"
        local_admin_8771 = "127.0.0.1:8771"
        localhost_http = "http://127.0.0.1:8770"
        lan_http = if ($LanIPv4) { "http://${LanIPv4}:8770" } else { "" }
        public_https = ConvertTo-AfpPublicEndpoint -Value $PublicUrl
        funnel_origin = ConvertTo-AfpPublicEndpoint -Value $FunnelOrigin
        static_identity = $ExpectedCandidateId
    }
    $checks = [Collections.Generic.List[object]]::new()
    $issues = [Collections.Generic.List[string]]::new()
    foreach ($entry in $targets.GetEnumerator()) {
        if ($ProbeRunner) {
            $probe = & $ProbeRunner ([string]$entry.Key) ([string]$entry.Value)
        } else {
            $passed = $false
            $probeDetail = ""
            try {
                switch ([string]$entry.Key) {
                    "listener_8770" {
                        $connection = @(Get-NetTCPConnection -State Listen -LocalPort 8770 -ErrorAction Stop | Select-Object -First 1)
                        if ($connection.Count) {
                            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection[0].OwningProcess)" -ErrorAction Stop
                            $passed = [string]::Equals([string]$process.ExecutablePath, [string]$entry.Value, [StringComparison]::OrdinalIgnoreCase)
                        }
                    }
                    "local_admin_8771" {
                        $connections = @(Get-NetTCPConnection -State Listen -LocalPort 8771 -ErrorAction Stop)
                        $passed = $connections.Count -gt 0 -and @($connections | Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") }).Count -eq 0
                    }
                    "localhost_http" { $passed = (Invoke-WebRequest -UseBasicParsing -Uri ([string]$entry.Value) -TimeoutSec 8).StatusCode -eq 200 }
                    "lan_http" { $passed = [bool]$entry.Value -and (Invoke-WebRequest -UseBasicParsing -Uri ([string]$entry.Value) -TimeoutSec 8).StatusCode -eq 200 }
                    "public_https" { $passed = [bool]$entry.Value -and (Invoke-WebRequest -UseBasicParsing -Uri ([string]$entry.Value) -TimeoutSec 12).StatusCode -eq 200 }
                    "funnel_origin" { $passed = [string]::Equals([string]$entry.Value, "http://127.0.0.1:8770", [StringComparison]::OrdinalIgnoreCase) }
                    "static_identity" {
                        $identityProbe = Test-AfpPublicStaticIdentity -RuntimeRoot $RuntimeRoot -PublicUrl $PublicUrl -ExpectedCandidateId $ExpectedCandidateId
                        $passed = [bool]$identityProbe.passed
                        $probeDetail = [string]$identityProbe.detail
                    }
                }
            } catch { $passed = $false }
            $probe = [pscustomobject]@{ passed = $passed; detail = if ($probeDetail) { $probeDetail } elseif ($passed) { "ok" } else { "probe failed" } }
        }
        $checks.Add([pscustomobject]@{ name = [string]$entry.Key; passed = [bool]$probe.passed; detail = [string]$probe.detail })
        if (-not $probe.passed) { $issues.Add("service probe failed: $($entry.Key)") }
    }
    return [pscustomobject][ordered]@{
        status = if ($issues.Count -eq 0) { "passed" } else { "failed" }
        checks = @($checks)
        issues = @($issues)
    }
}

function Wait-AfpRuntimeServices {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [string]$PublicUrl = "",
        [string]$LanIPv4 = "",
        [string]$ExpectedCandidateId = "",
        [string]$FunnelOrigin = "",
        [ValidateRange(0, 600)][int]$TimeoutSeconds = 90,
        [ValidateRange(1, 60000)][int]$PollIntervalMilliseconds = 1000,
        [scriptblock]$ProbeRunner,
        [scriptblock]$DelayRunner
    )

    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $attempts = 0
    do {
        $attempts++
        $arguments = @{
            RuntimeRoot = $RuntimeRoot
            PublicUrl = $PublicUrl
            LanIPv4 = $LanIPv4
            ExpectedCandidateId = $ExpectedCandidateId
            FunnelOrigin = $FunnelOrigin
        }
        if ($ProbeRunner) { $arguments.ProbeRunner = $ProbeRunner }
        $result = Test-AfpRuntimeServices @arguments
        if ($result.status -eq "passed") { break }
        if ($stopwatch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
        if ($DelayRunner) { & $DelayRunner $PollIntervalMilliseconds }
        else { Start-Sleep -Milliseconds $PollIntervalMilliseconds }
    } while ($true)
    $stopwatch.Stop()

    return [pscustomobject][ordered]@{
        status = [string]$result.status
        attempts = $attempts
        elapsed_milliseconds = [int64]$stopwatch.ElapsedMilliseconds
        checks = @($result.checks)
        issues = @($result.issues)
    }
}

function Get-AfpStartupRepairPreview {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Inventory,
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [object[]]$StartupRecords
    )
    if (-not $PSBoundParameters.ContainsKey("StartupRecords")) {
        $StartupRecords = @()
        $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        $run = Get-ItemProperty -LiteralPath $runKey -ErrorAction SilentlyContinue
        if ($run) {
            foreach ($property in $run.PSObject.Properties | Where-Object { $_.Name -like "AFP*" }) {
                $StartupRecords += [pscustomobject]@{ Name = $property.Name; Value = [string]$property.Value; Source = "HKCU Run" }
            }
        }
        foreach ($task in @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "AFP*" })) {
            foreach ($action in @($task.Actions)) {
                $StartupRecords += [pscustomobject]@{
                    Name = $task.TaskName
                    Value = (([string]$action.Execute) + " " + ([string]$action.Arguments)).Trim()
                    TargetPath = [string]$action.Execute
                    Source = "ScheduledTask"
                }
            }
        }
    }
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $changes = [Collections.Generic.List[object]]::new()
    $issues = [Collections.Generic.List[string]]::new()
    foreach ($record in $StartupRecords) {
        $name = [string]$record.Name
        $source = [string]$record.Source
        $oldValue = if ($record.PSObject.Properties.Name -contains "Value") { [string]$record.Value } else { "" }
        if ($oldValue -match '(?i)(password|token|pair(?:ing)?[_-]?code|credential|secret)') {
            $issues.Add("startup value contains sensitive arguments: $name")
            continue
        }
        $newValue = ""
        $action = "set"
        switch ($name) {
            "AFP_Tailscale_Funnel_Watchdog" {
                $script = Join-Path $Paths.RuntimeRoot "tailscale_funnel_watchdog.ps1"
                $newValue = "$powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Mode Run"
            }
            "AFP_Public_Tunnel_Watchdog" { $action = "remove" }
            "AFP Local Capture Helper" {
                $helper = Join-Path $Paths.RuntimeRoot "local_helper\AFP_Local_Capture_Helper.exe"
                $newValue = "`"$helper`" --background"
            }
            default { continue }
        }
        if ($action -eq "remove" -or $oldValue -ne $newValue) {
            $changes.Add([pscustomobject][ordered]@{
                name = $name
                source = $source
                action = $action
                old_value = $oldValue
                new_value = $newValue
                recovery_value = $oldValue
            })
        }
    }
    $stable = [ordered]@{ inventory_digest = [string]$Inventory.digest; changes = @($changes); issues = @($issues) }
    $digest = Get-AfpTextSha256 -Text ($stable | ConvertTo-Json -Depth 10 -Compress)
    return [pscustomobject][ordered]@{
        status = if ($issues.Count) { "blocked" } elseif ($changes.Count) { "approval_required" } else { "passed" }
        inventory_digest = [string]$Inventory.digest
        changes = @($changes)
        issues = @($issues)
        digest = $digest
    }
}

function Set-AfpApprovedStartupTargets {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Preview,
        [Parameter(Mandatory = $true)][string]$PreviewDigest,
        [scriptblock]$ApplyRunner
    )
    if ($Preview.status -ne "approval_required") { throw "Startup repair preview is not approved for application." }
    if (-not [string]::Equals([string]$Preview.digest, $PreviewDigest, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Startup repair preview changed after approval."
    }
    foreach ($change in @($Preview.changes)) {
        if ($ApplyRunner) {
            & $ApplyRunner $change
            continue
        }
        if ($change.source -eq "HKCU Run") {
            $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
            if ($change.action -eq "remove") {
                Remove-ItemProperty -LiteralPath $runKey -Name $change.name -ErrorAction SilentlyContinue
            } else {
                New-Item -Path $runKey -Force | Out-Null
                Set-ItemProperty -LiteralPath $runKey -Name $change.name -Value $change.new_value
            }
        } elseif ($change.name -eq "AFP Local Capture Helper") {
            $match = [regex]::Match([string]$change.new_value, '^"([^"]+AFP_Local_Capture_Helper\.exe)"')
            if (-not $match.Success) { throw "Helper startup target is invalid." }
            $runtime = Split-Path -Parent (Split-Path -Parent $match.Groups[1].Value)
            & (Join-Path $runtime "install_local_helper_autostart.ps1")
            if ($LASTEXITCODE -ne 0) { throw "Helper startup task update failed." }
        } else {
            throw "Unsupported startup target: $($change.name)"
        }
    }
    return [pscustomobject]@{ status = "passed"; applied = @($Preview.changes).Count }
}

Export-ModuleMember -Function @(
    "Get-AfpRuntimeInventory",
    "Stop-AfpManagedRuntimeProcesses",
    "Start-AfpManagedRuntimeProcesses",
    "Get-AfpMigrationInventory",
    "Test-AfpInventoryUnchanged",
    "New-AfpApprovalSummary",
    "Invoke-AfpRuntimePromotion",
    "Restore-AfpRuntimePromotion",
    "Complete-AfpRuntimePromotion",
    "Test-AfpPublicStaticIdentity",
    "Test-AfpRuntimeServices",
    "Wait-AfpRuntimeServices",
    "Get-AfpStartupRepairPreview",
    "Set-AfpApprovedStartupTargets"
)
