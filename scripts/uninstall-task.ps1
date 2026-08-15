[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Task = Get-ScheduledTask -TaskName "Quant Guardian" -ErrorAction SilentlyContinue
if ($Task) {
    Unregister-ScheduledTask -TaskName "Quant Guardian" -Confirm:$false
    Write-Host "Scheduled task removed." -ForegroundColor Green
} else {
    Write-Host "Scheduled task was not installed."
}