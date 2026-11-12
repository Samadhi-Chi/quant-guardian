[CmdletBinding()]
param(
    [string]$Executable = "",
    [string]$Config = "",
    [string]$DailyStart = "08:20",
    [switch]$RunElevated
)

$ErrorActionPreference = "Stop"
if ($RunElevated -and -not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Elevated startup must be explicitly installed from an administrator PowerShell."
}
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $Config) {
    $Config = Join-Path $env:LOCALAPPDATA "QuantGuardian\config\quant-guardian.json"
}
$Config = [IO.Path]::GetFullPath($Config)

$Arguments = ""
$WorkingDirectory = ""
if (-not $Executable) {
    $Candidates = @(
        (Join-Path $ProjectRoot "Quant Guardian\Quant Guardian.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Quant Guardian\Quant Guardian.exe"),
        (Join-Path $ProjectRoot "dist\Quant Guardian\Quant Guardian.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            $Executable = $Candidate
            break
        }
    }
}
if ($Executable) {
    $Executable = [IO.Path]::GetFullPath($Executable)
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Quant Guardian executable was not found: $Executable"
    }
    $Arguments = '--watchdog --config "' + $Config + '"'
    $WorkingDirectory = Split-Path -Parent $Executable
} else {
    $Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) {
        throw "No installed executable or development virtual environment was found."
    }
    $Executable = $Pythonw
    $Arguments = '-m quant_guardian --config "' + $Config + '"'
    $WorkingDirectory = $ProjectRoot
}

$Action = New-ScheduledTaskAction -Execute $Executable -Argument $Arguments -WorkingDirectory $WorkingDirectory
$Triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Daily -At $DailyStart),
    (New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval ([TimeSpan]::FromMinutes(1)) `
        -RepetitionDuration ([TimeSpan]::FromDays(3650)))
)
$TaskRunLevel = if ($RunElevated) { "Highest" } else { "Limited" }
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel $TaskRunLevel
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval ([TimeSpan]::FromMinutes(1))
$Task = New-ScheduledTask -Action $Action -Trigger $Triggers -Principal $Principal -Settings $Settings -Description "Quant Guardian QMT monitor. Automatic actions remain controlled by the independent recovery authorization."
Register-ScheduledTask -TaskName "Quant Guardian" -InputObject $Task -Force | Out-Null
Write-Host "Scheduled task installed for the current interactive user." -ForegroundColor Green
Write-Host "Executable: $Executable"
Write-Host "Triggers: at logon, daily at $DailyStart, and a one-minute missing-process safety net."
Write-Host "This script does not enable live QMT recovery."
