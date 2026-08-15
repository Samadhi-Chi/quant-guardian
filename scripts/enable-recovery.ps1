[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$Until = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
if (-not $Config) {
    $Config = Join-Path $env:LOCALAPPDATA "QuantGuardian\config\quant-guardian.json"
}
$Config = [IO.Path]::GetFullPath($Config)
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    throw "Configuration does not exist: $Config. Start Quant Guardian once first."
}
if (-not $Force) {
    Write-Host "LIVE QMT RECOVERY AUTHORIZATION" -ForegroundColor Yellow
    Write-Host "This enables Quant Guardian to close and restart validated QMT processes."
    Write-Host "It never enables Rocket auto-resume and never enables order/cancel calls."
    $Answer = Read-Host "Type ENABLE to continue"
    if ($Answer -cne "ENABLE") {
        throw "Recovery authorization cancelled."
    }
}

$Document = Get-Content -Raw -Encoding UTF8 -LiteralPath $Config | ConvertFrom-Json
$Document.mode = "recover"
if ($Until) {
    $Deadline = [DateTimeOffset]::Parse($Until)
    if (-not $Document.recovery) {
        throw "Recovery configuration is missing."
    }
    $Document.recovery | Add-Member -NotePropertyName automatic_recovery_until -NotePropertyValue $Deadline.ToString("o") -Force
} elseif ($Document.recovery) {
    $Document.recovery | Add-Member -NotePropertyName automatic_recovery_until -NotePropertyValue "" -Force
}
$Json = $Document | ConvertTo-Json -Depth 20
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$Temporary = "$Config.tmp"
[IO.File]::WriteAllText($Temporary, $Json + [Environment]::NewLine, $Utf8)
Move-Item -LiteralPath $Temporary -Destination $Config -Force

$StateDirectory = Join-Path $env:LOCALAPPDATA "QuantGuardian\state"
[IO.Directory]::CreateDirectory($StateDirectory) | Out-Null
$Sentinel = Join-Path $StateDirectory "RECOVERY_ENABLED"
[IO.File]::WriteAllText(
    $Sentinel,
    "QUANT_GUARDIAN_LIVE_RECOVERY_V1" + [Environment]::NewLine,
    $Utf8
)
if ($Until) {
    Write-Host "Live QMT recovery is authorized until $($Deadline.ToString('o')). Restart Quant Guardian." -ForegroundColor Green
} else {
    Write-Host "Live QMT recovery is authorized. Restart Quant Guardian." -ForegroundColor Green
}
