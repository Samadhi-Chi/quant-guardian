[CmdletBinding()]
param(
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
if (-not $Config) {
    $Config = Join-Path $env:LOCALAPPDATA "QuantGuardian\config\quant-guardian.json"
}
$Config = [IO.Path]::GetFullPath($Config)
$Sentinel = Join-Path $env:LOCALAPPDATA "QuantGuardian\state\RECOVERY_ENABLED"
if (Test-Path -LiteralPath $Sentinel) {
    Remove-Item -LiteralPath $Sentinel -Force
}
if (Test-Path -LiteralPath $Config -PathType Leaf) {
    $Document = Get-Content -Raw -Encoding UTF8 -LiteralPath $Config | ConvertFrom-Json
    $Document.mode = "observe"
    if ($Document.recovery) {
        $Document.recovery | Add-Member -NotePropertyName automatic_recovery_until -NotePropertyValue "" -Force
    }
    $Utf8 = New-Object System.Text.UTF8Encoding($false)
    $Temporary = "$Config.tmp"
    [IO.File]::WriteAllText(
        $Temporary,
        ($Document | ConvertTo-Json -Depth 20) + [Environment]::NewLine,
        $Utf8
    )
    Move-Item -LiteralPath $Temporary -Destination $Config -Force
}
Write-Host "Live recovery disabled. Observation and alerts remain available." -ForegroundColor Green
