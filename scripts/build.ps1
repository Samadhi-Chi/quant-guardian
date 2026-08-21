[CmdletBinding()]
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$LocalPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not $Python) {
    if (Test-Path -LiteralPath $LocalPython -PathType Leaf) {
        $Python = $LocalPython
    } else {
        $PythonCommand = @(Get-Command python -CommandType Application -ErrorAction SilentlyContinue)[0]
        if ($PythonCommand) {
            $Python = [string]$PythonCommand.Source
        }
    }
}
$Spec = Join-Path $ProjectRoot "packaging\quant-guardian.spec"
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python 3.11-3.14 was not found. Run .\scripts\bootstrap.ps1 -Dev or pass -Python."
}
$RuntimeVersion = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0 -or $RuntimeVersion -notmatch '^3\.(11|12|13|14)$') {
    throw "Unsupported build Python: $RuntimeVersion. Expected Python 3.11-3.14."
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
$GatewayExecutable = Join-Path $ProjectRoot "dist\Quant Guardian\Quant Guardian Gateway.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Build finished without the expected executable: $Executable"
}
if (-not (Test-Path -LiteralPath $GatewayExecutable -PathType Leaf)) {
    throw "Build finished without the expected gateway executable: $GatewayExecutable"
}
$Version = (& $Python -c "from quant_guardian import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) {
    throw "Unable to determine the Quant Guardian package version."
}
Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Executable) "VERSION") -Value $Version -Encoding ascii
$Hash = Get-FileHash -LiteralPath $Executable -Algorithm SHA256
$Hash | Format-List
$GatewayHash = Get-FileHash -LiteralPath $GatewayExecutable -Algorithm SHA256
$GatewayHash | Format-List
