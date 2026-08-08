param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$ArchivePath
)

$ErrorActionPreference = "Stop"
if (Test-Path -LiteralPath $ArchivePath) {
    Remove-Item -LiteralPath $ArchivePath -Force
}
Compress-Archive -Path (Join-Path $SourceDirectory "*") -DestinationPath $ArchivePath -CompressionLevel Optimal
Write-Host "Created release archive: $ArchivePath"
