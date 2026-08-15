[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$Destination = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Version = "250807.1.2"
$ExpectedSha256 = "91F19FF9A92971C5ABE64FBD077E5212E0418F0820AA3427AEF3444230F72921"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$DownloadDirectory = Join-Path $ProjectRoot ".downloads"

if (-not $PythonExe) {
    $PythonExe = Join-Path $env:LOCALAPPDATA "QuantGuardian\Python311\python.exe"
}
if (-not $Destination) {
    $Destination = Join-Path $env:LOCALAPPDATA "QuantGuardian\XtQuant"
}
$PythonExe = [IO.Path]::GetFullPath($PythonExe)
$Destination = [IO.Path]::GetFullPath($Destination)
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Quant Guardian private Python was not found: $PythonExe"
}

$Manifest = Join-Path $Destination "QUANT_GUARDIAN_XTQUANT.json"
if ((Test-Path -LiteralPath $Manifest -PathType Leaf) -and -not $Force) {
    $Metadata = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Metadata.version -eq $Version -and $Metadata.sha256 -eq $ExpectedSha256) {
        & $PythonExe -s -c "import sys; sys.path.insert(0, sys.argv[1]); from xtquant import xttrader, xttype; print('XtQuant import verified')" $Destination
        if ($LASTEXITCODE -eq 0) {
            Write-Host "XtQuant $Version is already ready: $Destination" -ForegroundColor Green
            exit 0
        }
    }
    throw "An unverified XtQuant installation already exists. Re-run with -Force to preserve it as a backup and replace it."
}
if ((Test-Path -LiteralPath $Destination) -and -not $Force) {
    throw "XtQuant destination already exists without the expected manifest: $Destination. Re-run with -Force to preserve it as a backup and replace it."
}

[IO.Directory]::CreateDirectory($DownloadDirectory) | Out-Null
& $PythonExe -m pip download --no-deps --only-binary=:all: --dest $DownloadDirectory "xtquant==$Version"
if ($LASTEXITCODE -ne 0) { throw "Unable to download XtQuant $Version from PyPI." }

$Wheel = Get-ChildItem -LiteralPath $DownloadDirectory -Filter "xtquant-$Version-*.whl" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $Wheel) { throw "The expected XtQuant wheel was not downloaded." }
$ActualSha256 = (Get-FileHash -LiteralPath $Wheel.FullName -Algorithm SHA256).Hash
if ($ActualSha256 -ne $ExpectedSha256) {
    throw "XtQuant wheel SHA256 mismatch. Expected $ExpectedSha256, got $ActualSha256"
}

$Parent = Split-Path -Parent $Destination
[IO.Directory]::CreateDirectory($Parent) | Out-Null
$Staging = Join-Path $Parent ("XtQuant.install-" + [Guid]::NewGuid().ToString("N"))
[IO.Directory]::CreateDirectory($Staging) | Out-Null
& $PythonExe -m pip install --no-index --no-deps --target $Staging $Wheel.FullName
if ($LASTEXITCODE -ne 0) {
    throw "XtQuant installation failed. The staging directory was left for inspection: $Staging"
}
& $PythonExe -s -c "import sys; sys.path.insert(0, sys.argv[1]); from xtquant import xttrader, xttype; print(xttrader.__file__)" $Staging
if ($LASTEXITCODE -ne 0) {
    throw "XtQuant import verification failed. The staging directory was left for inspection: $Staging"
}

$Metadata = [ordered]@{
    source = "https://pypi.org/project/xtquant/"
    version = $Version
    sha256 = $ActualSha256
    installed_at = [DateTimeOffset]::Now.ToString("o")
}
$Metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Staging "QUANT_GUARDIAN_XTQUANT.json") -Encoding UTF8

if (Test-Path -LiteralPath $Destination) {
    $Backup = $Destination + ".backup-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Move-Item -LiteralPath $Destination -Destination $Backup
    Write-Host "Previous XtQuant installation preserved at: $Backup" -ForegroundColor Yellow
}
Move-Item -LiteralPath $Staging -Destination $Destination
Write-Host "XtQuant $Version verified and installed: $Destination" -ForegroundColor Green
