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
$BasePrefix = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $BasePrefix -PathType Container)) {
    throw "Unable to resolve the build Python base prefix."
}
$OriginalPath = $env:Path
$TrustedPathEntries = @(
    (Split-Path -Parent $Python),
    $BasePrefix,
    (Join-Path $BasePrefix "DLLs"),
    (Join-Path $env:SystemRoot "System32"),
    $env:SystemRoot
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
# PyInstaller resolves transitive DLLs through PATH.  Desktop hosts may prepend
# unrelated native toolchains (for example Poppler or libheif), whose UCRT/ICU
# DLLs can shadow the Windows and Qt runtimes and create a package that builds
# successfully but cannot import QtCore.  Build with only trusted runtime roots.
$env:Path = $TrustedPathEntries -join [IO.Path]::PathSeparator
Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean $Spec
    $BuildExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    $env:Path = $OriginalPath
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
