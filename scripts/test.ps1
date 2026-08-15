[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "Python was not found. Run scripts\bootstrap.ps1 -Dev first."
    }
    $Python = $PythonCommand.Source
}
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
& $Python -m unittest discover -s (Join-Path $ProjectRoot "tests") -v
exit $LASTEXITCODE
