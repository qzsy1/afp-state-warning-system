Set-StrictMode -Version Latest

$script:AfpHighestIngressGeneration = @{}

function Test-AfpAddressInPrefix {
    param(
        [Parameter(Mandatory = $true)][Net.IPAddress]$Address,
        [Parameter(Mandatory = $true)][string]$Prefix
    )

    $parts = $Prefix.Split('/')
    $network = [Net.IPAddress]::Parse($parts[0])
    $prefixLength = [int]$parts[1]
    $addressBytes = $Address.GetAddressBytes()
    $networkBytes = $network.GetAddressBytes()
    if ($addressBytes.Length -ne $networkBytes.Length) {
        return $false
    }

    $wholeBytes = [Math]::Floor($prefixLength / 8)
    for ($index = 0; $index -lt $wholeBytes; $index++) {
        if ($addressBytes[$index] -ne $networkBytes[$index]) {
            return $false
        }
    }
    $remainingBits = $prefixLength % 8
    if ($remainingBits -eq 0) {
        return $true
    }
    $mask = (0xff -shl (8 - $remainingBits)) -band 0xff
    return (($addressBytes[$wholeBytes] -band $mask) -eq ($networkBytes[$wholeBytes] -band $mask))
}

function Test-AfpGlobalIPAddress {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Address)

    $candidate = $Address.Trim()
    if (-not $candidate -or $candidate.Contains('%')) {
        return $false
    }
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($candidate, [ref]$parsed)) {
        return $false
    }
    if ($parsed.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6 -and $parsed.IsIPv4MappedToIPv6) {
        return Test-AfpGlobalIPAddress -Address $parsed.MapToIPv4().ToString()
    }

    if ($parsed.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) {
        $globalExceptions = @('192.0.0.9/32', '192.0.0.10/32')
        foreach ($prefix in $globalExceptions) {
            if (Test-AfpAddressInPrefix -Address $parsed -Prefix $prefix) {
                return $true
            }
        }
        $specialPrefixes = @(
            '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
            '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
            '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
            '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4'
        )
    } elseif ($parsed.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) {
        $globalExceptions = @(
            '2001:1::1/128', '2001:1::2/128', '2001:1::3/128',
            '2001:3::/32', '2001:4:112::/48', '2001:30::/28'
        )
        foreach ($prefix in $globalExceptions) {
            if (Test-AfpAddressInPrefix -Address $parsed -Prefix $prefix) {
                return $true
            }
        }
        $specialPrefixes = @(
            '::/128', '::1/128', '64:ff9b:1::/48', '100::/64', '2001::/23',
            '2001:db8::/32', '2002::/16', '3fff::/20', '5f00::/16',
            'fc00::/7', 'fe80::/10', 'ff00::/8'
        )
    } else {
        return $false
    }

    foreach ($prefix in $specialPrefixes) {
        if (Test-AfpAddressInPrefix -Address $parsed -Prefix $prefix) {
            return $false
        }
    }
    return $true
}

function Resolve-AfpPublicEdgeAddresses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [scriptblock]$Resolver
    )

    $normalizedHost = $HostName.Trim().TrimEnd('.').ToLowerInvariant()
    if (-not $normalizedHost) {
        throw 'HostName must not be empty.'
    }
    if (-not $Resolver) {
        $Resolver = {
            param($QueryHost, $RecordType)
            $lastError = $null
            foreach ($server in @('1.1.1.1', '8.8.8.8')) {
                try {
                    return @(Resolve-DnsName -Name $QueryHost -Type $RecordType -Server $server -DnsOnly -ErrorAction Stop |
                        Where-Object { $_.IPAddress } |
                        ForEach-Object { [string]$_.IPAddress })
                } catch {
                    $lastError = $_
                }
            }
            throw "Public DNS resolution failed for $QueryHost ${RecordType}: $lastError"
        }
    }

    $seen = @{}
    $addresses = [Collections.Generic.List[string]]::new()
    $resolutionErrors = [Collections.Generic.List[string]]::new()
    foreach ($recordType in @('A', 'AAAA')) {
        try {
            $answers = @(& $Resolver $normalizedHost $recordType)
        } catch {
            $resolutionErrors.Add("${recordType}:$($_.Exception.Message)")
            continue
        }
        foreach ($answer in $answers) {
            $value = [string]$answer
            if ((Test-AfpGlobalIPAddress -Address $value) -and -not $seen.ContainsKey($value)) {
                $seen[$value] = $true
                $addresses.Add($value)
            }
        }
    }
    if (-not $addresses.Count -and $resolutionErrors.Count) {
        throw "Public DNS resolution produced no global addresses."
    }
    return @($addresses)
}

function Invoke-AfpForcedPublicProbe {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][Uri]$Uri,
        [Parameter(Mandatory = $true)][string]$Address,
        [string]$ExpectedSha256 = '',
        [scriptblock]$CurlRunner
    )

    if (-not $Uri.IsAbsoluteUri -or $Uri.Scheme -ne 'https' -or -not $Uri.DnsSafeHost) {
        throw 'Forced public probes require an absolute HTTPS URI.'
    }
    if (-not (Test-AfpGlobalIPAddress -Address $Address)) {
        throw 'Forced public probes require a globally routable address.'
    }
    if ($ExpectedSha256 -and $ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'ExpectedSha256 must contain exactly 64 hexadecimal characters.'
    }

    $port = if ($Uri.IsDefaultPort) { 443 } else { $Uri.Port }
    $resolvedAddress = if ($Address.Contains(':')) { "[$Address]" } else { $Address }
    $bodyPath = Join-Path ([IO.Path]::GetTempPath()) ("afp-public-probe-{0}.tmp" -f [Guid]::NewGuid().ToString('N'))
    $arguments = @(
        '--silent', '--show-error', '--output', $bodyPath,
        '--write-out', '%{http_code}',
        '--connect-timeout', '5', '--max-time', '15',
        '--max-redirs', '0', '--noproxy', '*',
        '--resolve', ("{0}:{1}:{2}" -f $Uri.DnsSafeHost.ToLowerInvariant(), $port, $resolvedAddress),
        $Uri.AbsoluteUri
    )

    try {
        if ($CurlRunner) {
            $execution = & $CurlRunner $arguments $bodyPath
        } else {
            $statusText = & curl.exe @arguments
            $execution = [pscustomobject]@{
                exit_code = $LASTEXITCODE
                status_code = if ($statusText -match '^\s*(\d{3})\s*$') { [int]$Matches[1] } else { 0 }
            }
        }
        $exitCode = [int]$execution.exit_code
        $statusCode = [int]$execution.status_code
        $hashMatches = $true
        if ($ExpectedSha256) {
            $hashMatches = (Test-Path -LiteralPath $bodyPath -PathType Leaf) -and
                ((Get-FileHash -LiteralPath $bodyPath -Algorithm SHA256).Hash -ieq $ExpectedSha256)
        }
        $ok = $exitCode -eq 0 -and $statusCode -eq 200 -and $hashMatches
        $errorCode = if ($exitCode -ne 0) {
            'transport_failed'
        } elseif ($statusCode -ne 200) {
            'unexpected_http_status'
        } elseif (-not $hashMatches) {
            'identity_hash_mismatch'
        } else {
            $null
        }
        return [pscustomobject]@{
            ok = $ok
            status_code = $statusCode
            address = $Address
            error = $errorCode
        }
    } finally {
        Remove-Item -LiteralPath $bodyPath -Force -ErrorAction SilentlyContinue
    }
}

function ConvertTo-AfpNormalizedPublicUrl {
    param([Parameter(Mandatory = $true)][string]$PublicUrl)

    $uri = $null
    if (-not [Uri]::TryCreate($PublicUrl.Trim(), [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne 'https' -or -not $uri.DnsSafeHost -or $uri.UserInfo -or $uri.Fragment) {
        throw 'PublicUrl must be an absolute HTTPS URL without credentials or a fragment.'
    }
    $builder = [UriBuilder]::new($uri)
    $builder.Scheme = 'https'
    $builder.Host = $uri.DnsSafeHost.ToLowerInvariant()
    if ($builder.Port -eq 443) {
        $builder.Port = -1
    }
    if ($builder.Path -eq '/') {
        $builder.Path = ''
    } else {
        $builder.Path = $builder.Path.TrimEnd('/')
    }
    return $builder.Uri.AbsoluteUri.TrimEnd('/')
}

function Get-AfpIngressStateKey {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [IO.Path]::GetFullPath($Path).TrimEnd('\').ToLowerInvariant()
}

function Assert-AfpIngressState {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$ExpectedCandidateId = ''
    )

    if ([int]$State.schema_version -ne 1) {
        throw 'Unsupported public ingress state schema_version.'
    }
    $generation = [long]$State.generation
    if ($generation -lt 0) {
        throw 'Public ingress generation must be nonnegative.'
    }
    $null = ConvertTo-AfpNormalizedPublicUrl -PublicUrl ([string]$State.public_url)
    foreach ($name in @('provider', 'recovery_owner', 'candidate_id', 'status', 'published_at')) {
        if (-not ([string]$State.$name).Trim()) {
            throw "Public ingress state field is empty: $name"
        }
    }
    if ($null -eq $State.layers) {
        throw 'Public ingress state layers are required.'
    }
    $publishedAt = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse([string]$State.published_at, [ref]$publishedAt)) {
        throw 'Public ingress published_at is invalid.'
    }
    if ($ExpectedCandidateId -and ([string]$State.candidate_id -cne $ExpectedCandidateId)) {
        throw 'Public ingress candidate_id does not match the expected candidate.'
    }

    $key = Get-AfpIngressStateKey -Path $Path
    if ($script:AfpHighestIngressGeneration.ContainsKey($key) -and
        $generation -lt [long]$script:AfpHighestIngressGeneration[$key]) {
        throw 'Public ingress generation rollback detected.'
    }
    $script:AfpHighestIngressGeneration[$key] = $generation
    return $State
}

function Read-AfpPublicIngressState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$ExpectedCandidateId = ''
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Public ingress state does not exist: $Path"
    }
    try {
        $state = [IO.File]::ReadAllText([IO.Path]::GetFullPath($Path), [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Public ingress state is corrupt: $($_.Exception.Message)"
    }
    return Assert-AfpIngressState -State $state -Path $Path -ExpectedCandidateId $ExpectedCandidateId
}

function Get-AfpIngressStatus {
    param([Parameter(Mandatory = $true)][hashtable]$Layers)

    if ($Layers.Count -eq 0) {
        return 'offline'
    }
    foreach ($layer in $Layers.Values) {
        if ($null -eq $layer -or -not [bool]$layer.ok) {
            return 'degraded'
        }
    }
    return 'online'
}

function Publish-AfpPublicIngressState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$PublicUrl,
        [Parameter(Mandatory = $true)][string]$Provider,
        [Parameter(Mandatory = $true)][string]$RecoveryOwner,
        [Parameter(Mandatory = $true)][string]$CandidateId,
        [Parameter(Mandatory = $true)][hashtable]$Layers,
        [long]$ExpectedGeneration,
        [string]$ExpectedPreviousCandidateId = '',
        [long]$MinimumGeneration = -1
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $directory = Split-Path -Parent $fullPath
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        [IO.Directory]::CreateDirectory($directory) | Out-Null
    }
    $normalizedUrl = ConvertTo-AfpNormalizedPublicUrl -PublicUrl $PublicUrl
    $normalizedProvider = $Provider.Trim().ToLowerInvariant()
    $normalizedOwner = $RecoveryOwner.Trim().ToLowerInvariant()
    $normalizedCandidate = $CandidateId.Trim()
    if (-not $normalizedProvider -or -not $normalizedOwner -or -not $normalizedCandidate) {
        throw 'Provider, RecoveryOwner, and CandidateId must not be empty.'
    }

    $key = Get-AfpIngressStateKey -Path $fullPath
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $keyHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key)))).Replace('-', '')
    } finally {
        $sha.Dispose()
    }
    $mutex = [Threading.Mutex]::new($false, "Global\AFP.PublicIngress.$keyHash")
    $acquired = $false
    $tempPath = Join-Path $directory (([IO.Path]::GetFileName($fullPath)) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupPath = Join-Path $directory (([IO.Path]::GetFileName($fullPath)) + '.' + [Guid]::NewGuid().ToString('N') + '.bak')
    try {
        try {
            $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds(10))
        } catch [Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw 'Timed out waiting for the public ingress state lock.'
        }

        $existing = $null
        if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
            $existing = Read-AfpPublicIngressState -Path $fullPath
            if (
                [string]$existing.candidate_id -cne $normalizedCandidate -and
                (
                    -not $ExpectedPreviousCandidateId -or
                    [string]$existing.candidate_id -cne $ExpectedPreviousCandidateId.Trim()
                )
            ) {
                throw 'Existing public ingress candidate_id differs from the requested candidate.'
            }
        }
        $currentGeneration = if ($existing) { [long]$existing.generation } else { 0L }
        if ($PSBoundParameters.ContainsKey('ExpectedGeneration') -and $ExpectedGeneration -ne $currentGeneration) {
            throw 'Public ingress generation changed before publication.'
        }
        $changed = $null -eq $existing -or
            ([string]$existing.public_url -cne $normalizedUrl) -or
            ([string]$existing.provider -cne $normalizedProvider) -or
            ([string]$existing.recovery_owner -cne $normalizedOwner) -or
            ([string]$existing.candidate_id -cne $normalizedCandidate)
        $generation = if ($changed) {
            [Math]::Max($currentGeneration + 1L, $MinimumGeneration)
        } else {
            [Math]::Max($currentGeneration, $MinimumGeneration)
        }
        if ($generation -gt $currentGeneration) { $changed = $true }
        $state = [ordered]@{
            schema_version = 1
            generation = $generation
            public_url = $normalizedUrl
            provider = $normalizedProvider
            recovery_owner = $normalizedOwner
            candidate_id = $normalizedCandidate
            status = Get-AfpIngressStatus -Layers $Layers
            published_at = [DateTimeOffset]::UtcNow.ToString('o')
            layers = $Layers
        }
        $json = $state | ConvertTo-Json -Depth 20
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream = [IO.FileStream]::new(
            $tempPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None,
            4096,
            [IO.FileOptions]::WriteThrough
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally {
            $stream.Dispose()
        }

        if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
            [IO.File]::Replace($tempPath, $fullPath, $backupPath, $true)
            Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        } else {
            [IO.File]::Move($tempPath, $fullPath)
        }
        $validated = Read-AfpPublicIngressState -Path $fullPath -ExpectedCandidateId $normalizedCandidate
        return [pscustomobject]@{
            changed = [bool]$changed
            generation = [long]$validated.generation
            state = $validated
        }
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
        if ($acquired) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

function Test-AfpIngressRecoveryOwner {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$Provider
    )

    $expected = $Provider.Trim().ToLowerInvariant()
    if (-not $expected) {
        return $false
    }
    return ([string]$State.provider).Trim().ToLowerInvariant() -ceq $expected -and
        ([string]$State.recovery_owner).Trim().ToLowerInvariant() -ceq $expected
}

Export-ModuleMember -Function @(
    'Test-AfpGlobalIPAddress',
    'Resolve-AfpPublicEdgeAddresses',
    'Invoke-AfpForcedPublicProbe',
    'Read-AfpPublicIngressState',
    'Publish-AfpPublicIngressState',
    'Test-AfpIngressRecoveryOwner'
)
