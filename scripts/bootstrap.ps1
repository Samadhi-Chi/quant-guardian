[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Venv = Join-Path $ProjectRoot ".venv"

function Resolve-Python {
    param([string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Python executable does not exist: $Requested"
        }
        return [IO.Path]::GetFullPath($Requested)
    }

    $Candidates = @()
    foreach ($Minor in @(14, 13, 12, 11)) {
        $Candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python3$Minor\python.exe"
        $Candidates += "C:\Python3$Minor\python.exe"
    }
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($Candidate)
        }
    }

    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        foreach ($Minor in @(14, 13, 12, 11)) {
            $Resolved = & $PyLauncher.Source "-3.$Minor" -c "import sys; print(sys.executable)"
            if ($LASTEXITCODE -eq 0 -and $Resolved) {
                return [IO.Path]::GetFullPath($Resolved.Trim())
            }
        }
    }
    throw @"
Python 3.11-3.14 x64 was not found.
Install a supported official x64 build, then run:
  .\scripts\bootstrap.ps1 -PythonExe C:\path\to\python.exe
The XTQuant native probe uses a separate private Python 3.11 runtime installed
by scripts\install-python-runtime.ps1.
"@
}

$Python = Resolve-Python $PythonExe
$Version = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print('64' if sys.maxsize > 2**32 else '32')"
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect Python: $Python" }
$Parts = @($Version)
$Supported = @("3.11", "3.12", "3.13", "3.14")
if ($Parts[0] -notin $Supported -or $Parts[1] -ne "64") {
    throw "Quant Guardian requires Python 3.11-3.14 x64. Found: $($Parts -join ' ')"
}

if (-not (Test-Path -LiteralPath $Venv)) {
    & $Python -m venv $Venv
}
$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
if ($Dev) {
    & $VenvPython -m pip install -e "$ProjectRoot[dev]"
} else {
    & $VenvPython -m pip install -e $ProjectRoot
}
& $VenvPython -m quant_guardian --simulate
Write-Host "Quant Guardian environment is ready: $Venv" -ForegroundColor Green
