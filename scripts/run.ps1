[CmdletBinding()]
param(
    [string]$Config = "",
    [switch]$Headless,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
}
$Arguments = @("-m", "quant_guardian")
if ($Config) { $Arguments += @("--config", [IO.Path]::GetFullPath($Config)) }
if ($Headless) { $Arguments += "--headless" }
if ($Once) { $Arguments += "--once" }
& $Python @Arguments
exit $LASTEXITCODE