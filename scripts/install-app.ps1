[CmdletBinding()]
param(
    [string]$SourceDirectory = "",
    [string]$Destination = "",
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Import-Module (Join-Path $PSScriptRoot "InstallSafety.psm1") -Force
if (-not $SourceDirectory) {
    $SourceDirectory = Join-Path $ProjectRoot "dist\Quant Guardian"
}
if (-not $Destination) {
    $Destination = Join-Path $env:LOCALAPPDATA "Programs\Quant Guardian"
}
$SourceDirectory = [IO.Path]::GetFullPath($SourceDirectory)
$Destination = Assert-QgSafeDestination -Destination $Destination -ProjectRoot $ProjectRoot
$SourceExecutable = Join-Path $SourceDirectory "Quant Guardian.exe"
$SourceGatewayExecutable = Join-Path $SourceDirectory "Quant Guardian Gateway.exe"
if (-not (Test-Path -LiteralPath $SourceExecutable -PathType Leaf)) {
    throw "Built application was not found: $SourceExecutable. Run scripts\build.ps1 first."
}
if (-not (Test-Path -LiteralPath $SourceGatewayExecutable -PathType Leaf)) {
    throw "Gateway executable was not found: $SourceGatewayExecutable. Use the complete release package."
}

$Parent = Split-Path -Parent $Destination
[IO.Directory]::CreateDirectory($Parent) | Out-Null
$Staging = Join-Path $Parent ("Quant Guardian.install-" + [Guid]::NewGuid().ToString("N"))
Copy-Item -LiteralPath $SourceDirectory -Destination $Staging -Recurse
$StagedExecutable = Join-Path $Staging "Quant Guardian.exe"
$StagedGatewayExecutable = Join-Path $Staging "Quant Guardian Gateway.exe"
if (-not (Test-Path -LiteralPath $StagedExecutable -PathType Leaf)) {
    throw "Staged executable is missing: $StagedExecutable"
}
if (-not (Test-Path -LiteralPath $StagedGatewayExecutable -PathType Leaf)) {
    throw "Staged gateway executable is missing: $StagedGatewayExecutable"
}
$SourceHash = (Get-FileHash -LiteralPath $SourceExecutable -Algorithm SHA256).Hash
$StagedHash = (Get-FileHash -LiteralPath $StagedExecutable -Algorithm SHA256).Hash
$SourceGatewayHash = (Get-FileHash -LiteralPath $SourceGatewayExecutable -Algorithm SHA256).Hash
$StagedGatewayHash = (Get-FileHash -LiteralPath $StagedGatewayExecutable -Algorithm SHA256).Hash
if ($SourceHash -ne $StagedHash) {
    throw "Staged executable hash does not match the build output."
}
if ($SourceGatewayHash -ne $StagedGatewayHash) {
    throw "Staged gateway executable hash does not match the build output."
}
$VersionFile = Join-Path $SourceDirectory "VERSION"
if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
    throw "Build version metadata is missing: $VersionFile"
}
$Version = (Get-Content -LiteralPath $VersionFile -Raw -Encoding utf8).Trim()
if (-not $Version) {
    throw "Build version metadata is empty: $VersionFile"
}
Write-QgInstallMarker `
    -Directory $Staging `
    -InstallRoot $Destination `
    -Version $Version `
    -ExecutableSha256 $StagedHash | Out-Null

if (Test-Path -LiteralPath $Destination) {
    $Backup = $Destination + ".backup-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Move-Item -LiteralPath $Destination -Destination $Backup
    Write-Host "Previous application preserved at: $Backup" -ForegroundColor Yellow
}
Move-Item -LiteralPath $Staging -Destination $Destination

if (-not $NoShortcut) {
    $StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    [IO.Directory]::CreateDirectory($StartMenu) | Out-Null
    $ShortcutPath = Join-Path $StartMenu "Quant Guardian.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = Join-Path $Destination "Quant Guardian.exe"
    $Shortcut.WorkingDirectory = $Destination
    $Shortcut.Description = "Quant Guardian QMT health monitor"
    $Shortcut.Save()
}

Write-Host "Quant Guardian installed: $Destination" -ForegroundColor Green
Write-Host "SHA256: $SourceHash"
Write-Host "Version: $Version"
Write-Host "Live recovery remains disabled unless separately authorized."
