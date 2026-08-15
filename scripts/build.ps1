[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Spec = Join-Path $ProjectRoot "packaging\quant-guardian.spec"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 -Dev first."
}
Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean $Spec
    $BuildExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($BuildExitCode -ne 0) { exit $BuildExitCode }
$Executable = Join-Path $ProjectRoot "dist\Quant Guardian\Quant Guardian.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Build finished without the expected executable: $Executable"
}
$Version = (& $Python -c "from quant_guardian import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) {
    throw "Unable to determine the Quant Guardian package version."
}
Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Executable) "VERSION") -Value $Version -Encoding ascii
$Hash = Get-FileHash -LiteralPath $Executable -Algorithm SHA256
$Hash | Format-List
