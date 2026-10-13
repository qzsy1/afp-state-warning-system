Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot "SingleRuntimePaths.psm1")

function Get-AfpEvidenceSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-AfpEvidenceTextSha256 {
    param([Parameter(Mandatory = $true)][string]$Text)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace("-", "").ToLowerInvariant()
    } finally { $algorithm.Dispose() }
}

function Find-AfpSecretIndicators {
    param([Parameter(Mandatory = $true)][string]$Root)
    $findings = [Collections.Generic.List[object]]::new()
    $pattern = '(?i)(password|token|pairing[_-]?code|mysql[_-]?password|tailscale[_-]?(?:authkey|key|token))\s*[:=]\s*["'']?(?!redacted|null|none|false|true)([A-Za-z0-9+/_\-.]{4,})'
    foreach ($file in @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force)) {
        try {
            $bytes = [IO.File]::ReadAllBytes($file.FullName)
            $representations = @(
                [Text.Encoding]::UTF8.GetString($bytes),
                [Text.Encoding]::Unicode.GetString($bytes),
                [Text.Encoding]::BigEndianUnicode.GetString($bytes)
            )
            foreach ($text in $representations) {
                $match = [regex]::Match($text, $pattern)
                if ($match.Success) {
                    $findings.Add([pscustomobject]@{
                        path = $file.FullName.Substring($Root.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
                        rule = "credential_assignment"
                    })
                    break
                }
            }
        } catch {
            $findings.Add([pscustomobject]@{
                path = $file.FullName.Substring($Root.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
                rule = "unreadable_file"
            })
        }
    }
    return @($findings)
}

function Test-AfpEvidenceManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Destination)

    $destinationRoot = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
    $manifestPath = Join-Path $destinationRoot "migration-manifest.json"
    $issues = [Collections.Generic.List[object]]::new()
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        $issues.Add([pscustomobject]@{ path = "migration-manifest.json"; rule = "manifest_missing" })
    } else {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            foreach ($record in @($manifest.files)) {
                $relative = [string]$record.path
                if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains("..")) {
                    $issues.Add([pscustomobject]@{ path = $relative; rule = "invalid_relative_path" })
                    continue
                }
                $filePath = Join-Path $destinationRoot $relative.Replace('/', '\')
                if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
                    $issues.Add([pscustomobject]@{ path = $relative; rule = "file_missing" })
                    continue
                }
                $actual = Get-AfpEvidenceSha256 -Path $filePath
                if ($actual -ne [string]$record.sha256) {
                    $issues.Add([pscustomobject]@{ path = $relative; rule = "sha256_mismatch" })
                }
            }
        } catch {
            $issues.Add([pscustomobject]@{ path = "migration-manifest.json"; rule = "manifest_invalid" })
        }
    }
    return [pscustomobject]@{
        status = if ($issues.Count) { "failed" } else { "passed" }
        allow_source_delete = ($issues.Count -eq 0)
        issues = @($issues)
    }
}

function Copy-AfpEvidenceSafely {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][psobject]$Paths
    )
    $sourceRoot = [IO.Path]::GetFullPath($Source).TrimEnd('\')
    $destinationRoot = Assert-AfpMutableTarget -Path $Destination -Paths $Paths -Kind Evidence
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) { throw "Evidence source does not exist." }
    if (Test-Path -LiteralPath $destinationRoot) { throw "Evidence destination already exists." }
    $findings = @(Find-AfpSecretIndicators -Root $sourceRoot)
    if ($findings.Count) {
        return [pscustomobject]@{ status = "failed"; allow_source_delete = $false; files = @(); issues = $findings }
    }
    $records = [Collections.Generic.List[object]]::new()
    try {
        New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
        foreach ($sourceFile in @(Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -Force | Sort-Object FullName)) {
            $relative = $sourceFile.FullName.Substring($sourceRoot.Length).TrimStart('\')
            $targetFile = Join-Path $destinationRoot $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $targetFile) -Force | Out-Null
            Copy-Item -LiteralPath $sourceFile.FullName -Destination $targetFile
            $sourceHash = Get-AfpEvidenceSha256 -Path $sourceFile.FullName
            $targetHash = Get-AfpEvidenceSha256 -Path $targetFile
            if ($sourceHash -ne $targetHash) { throw "Evidence copy hash mismatch: $relative" }
            $records.Add([pscustomobject][ordered]@{
                path = $relative.Replace('\', '/')
                size = [long]$sourceFile.Length
                sha256 = $sourceHash
            })
        }
        $manifest = [pscustomobject][ordered]@{
            schema_version = 1
            source = $sourceRoot
            destination = $destinationRoot
            copied_at_utc = (Get-Date).ToUniversalTime().ToString("o")
            sanitized = $true
            files = @($records)
        }
        $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $destinationRoot "migration-manifest.json") -Encoding UTF8
        $verification = Test-AfpEvidenceManifest -Destination $destinationRoot
        if ($verification.status -ne "passed") { throw "Evidence manifest verification failed." }
        return [pscustomobject]@{ status = "passed"; allow_source_delete = $true; files = @($records); issues = @(); destination = $destinationRoot }
    } catch {
        if (Test-Path -LiteralPath $destinationRoot) { Remove-Item -LiteralPath $destinationRoot -Recurse -Force }
        return [pscustomobject]@{ status = "failed"; allow_source_delete = $false; files = @(); issues = @([pscustomobject]@{ path = ""; rule = "copy_or_hash_failed" }) }
    }
}

function Test-AfpPathWithin {
    param([string]$Path, [string]$Root)
    $candidate = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    $parent = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    return $candidate.StartsWith($parent, [StringComparison]::OrdinalIgnoreCase)
}

function Get-AfpCleanupPreview {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Targets,
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)][psobject]$Inventory,
        [string[]]$AllowedExternalTargets = @()
    )
    $items = [Collections.Generic.List[object]]::new()
    foreach ($rawTarget in $Targets) {
        $issues = [Collections.Generic.List[string]]::new()
        $resolved = ""
        if ([string]::IsNullOrWhiteSpace($rawTarget) -or [Management.Automation.WildcardPattern]::ContainsWildcardCharacters($rawTarget)) {
            $issues.Add("target is empty or contains a wildcard")
        } else {
            try { $resolved = [IO.Path]::GetFullPath($rawTarget).TrimEnd('\') } catch { $issues.Add("target path cannot be resolved") }
        }
        $externalAllowed = $false
        if ($resolved) {
            $insideWorkspace = Test-AfpPathWithin -Path $resolved -Root $Paths.WorkspaceRoot
            if ($insideWorkspace) {
                if (Test-AfpProtectedTarget -Path $resolved -Paths $Paths) { $issues.Add("target is protected") }
            } else {
                $externalAllowed = @($AllowedExternalTargets | Where-Object { [string]::Equals([IO.Path]::GetFullPath($_).TrimEnd('\'), $resolved, [StringComparison]::OrdinalIgnoreCase) }).Count -eq 1
                if (-not $externalAllowed) { $issues.Add("target is outside the AFP workspace and not explicitly allowlisted") }
            }
            foreach ($process in @($Inventory.processes)) {
                if ($process.executable_path -and ((Test-AfpPathWithin -Path $process.executable_path -Root $resolved) -or [string]::Equals([IO.Path]::GetFullPath($process.executable_path), $resolved, [StringComparison]::OrdinalIgnoreCase))) {
                    $issues.Add("target is referenced by active process $($process.pid)")
                }
            }
            foreach ($startup in @($Inventory.startup)) {
                if ($startup.target_path -and ((Test-AfpPathWithin -Path $startup.target_path -Root $resolved) -or [string]::Equals([IO.Path]::GetFullPath($startup.target_path), $resolved, [StringComparison]::OrdinalIgnoreCase))) {
                    $issues.Add("target is referenced by startup item $($startup.name)")
                }
            }
        }
        $size = 0L
        if ($resolved -and (Test-Path -LiteralPath $resolved)) {
            if (Test-Path -LiteralPath $resolved -PathType Leaf) { $size = [long](Get-Item -LiteralPath $resolved).Length }
            else {
                foreach ($file in @(Get-ChildItem -LiteralPath $resolved -Recurse -File -Force -ErrorAction SilentlyContinue)) {
                    $size += [long]$file.Length
                }
            }
        }
        $items.Add([pscustomobject][ordered]@{
            path = $resolved
            status = if ($issues.Count) { "blocked" } else { "ready" }
            size_bytes = $size
            external_allowlisted = $externalAllowed
            issues = @($issues)
        })
    }
    $stable = [ordered]@{ inventory_digest = [string]$Inventory.digest; targets = @($items) }
    $digest = Get-AfpEvidenceTextSha256 -Text ($stable | ConvertTo-Json -Depth 10 -Compress)
    $totalBytes = 0L
    foreach ($item in $items) { $totalBytes += [long]$item.size_bytes }
    return [pscustomobject][ordered]@{
        status = if (@($items | Where-Object { $_.status -eq "blocked" }).Count) { "blocked" } else { "approval_required" }
        inventory_digest = [string]$Inventory.digest
        targets = @($items)
        total_bytes = $totalBytes
        digest = $digest
    }
}

function Remove-AfpApprovedTargets {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$Preview,
        [Parameter(Mandatory = $true)][string]$PreviewDigest,
        [Parameter(Mandatory = $true)][psobject]$Paths,
        [Parameter(Mandatory = $true)][psobject]$CurrentInventory
    )
    if ($Preview.status -ne "approval_required") { throw "Cleanup preview is not ready for approval." }
    if (-not [string]::Equals([string]$Preview.digest, $PreviewDigest, [StringComparison]::OrdinalIgnoreCase)) { throw "Cleanup preview changed after approval." }
    if (-not [string]::Equals([string]$Preview.inventory_digest, [string]$CurrentInventory.digest, [StringComparison]::OrdinalIgnoreCase)) { throw "Runtime inventory changed after cleanup approval." }
    $deleted = [Collections.Generic.List[string]]::new()
    foreach ($target in @($Preview.targets)) {
        if ($target.status -ne "ready" -or -not $target.path) { throw "Cleanup contains an unapproved target." }
        if (-not $target.external_allowlisted) {
            Assert-AfpMutableTarget -Path $target.path -Paths $Paths -Kind Cleanup | Out-Null
        }
        if (Test-Path -LiteralPath $target.path) {
            Remove-Item -LiteralPath $target.path -Recurse -Force
            $deleted.Add([string]$target.path)
        }
    }
    return [pscustomobject]@{ status = "passed"; deleted = @($deleted); released_bytes = [long]$Preview.total_bytes }
}

function Write-AfpSingleRuntimeReport {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][psobject]$State,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][psobject]$Paths
    )
    $output = Assert-AfpMutableTarget -Path $OutputPath -Paths $Paths -Kind Evidence
    $validStatuses = @("passed", "failed", "blocked", "physical_sensor_pending")
    foreach ($name in @("pairing", "remote_helper", "simulation", "physical_sensor", "http_websocket", "mysql_reconciliation")) {
        if ($State.PSObject.Properties.Name -contains $name) {
            $value = [string]$State.$name
            if ($value -notin $validStatuses) { throw "Invalid acceptance status for ${name}: $value" }
        }
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $output) -Force | Out-Null
    $report = [ordered]@{ schema_version = 1; generated_at_utc = (Get-Date).ToUniversalTime().ToString("o") }
    foreach ($property in $State.PSObject.Properties) { $report[$property.Name] = $property.Value }
    $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $output -Encoding UTF8
    return $output
}

Export-ModuleMember -Function @(
    "Copy-AfpEvidenceSafely",
    "Test-AfpEvidenceManifest",
    "Get-AfpCleanupPreview",
    "Remove-AfpApprovedTargets",
    "Write-AfpSingleRuntimeReport"
)
