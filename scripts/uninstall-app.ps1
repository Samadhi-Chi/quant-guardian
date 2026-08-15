[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
    [string]$Destination = "",
    [switch]$KeepRegistration
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Import-Module (Join-Path $PSScriptRoot "InstallSafety.psm1") -Force

if (-not $Destination) {
    $Destination = Join-Path $env:LOCALAPPDATA "Programs\Quant Guardian"
}
$Destination = Assert-QgValidInstallation `
    -Destination $Destination `
    -ProjectRoot $ProjectRoot

if (-not $KeepRegistration) {
    if ($PSCmdlet.ShouldProcess("Scheduled task 'Quant Guardian'", "Remove registration")) {
        $Task = Get-ScheduledTask -TaskName "Quant Guardian" -ErrorAction SilentlyContinue
        if ($Task) {
            Unregister-ScheduledTask -TaskName "Quant Guardian" -Confirm:$false
        }
    }
    $Shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Quant Guardian.lnk"
    if (
        (Test-Path -LiteralPath $Shortcut -PathType Leaf) -and
        $PSCmdlet.ShouldProcess($Shortcut, "Remove Start menu shortcut")
    ) {
        Remove-Item -LiteralPath $Shortcut -Force
    }
}

if ($PSCmdlet.ShouldProcess($Destination, "Recursively remove verified Quant Guardian installation")) {
    Remove-Item -LiteralPath $Destination -Recurse -Force
    Write-Host "Quant Guardian application files were removed: $Destination" -ForegroundColor Green
}
Write-Host "Configuration, audit logs, private Python, and XtQuant were retained under %LOCALAPPDATA%\QuantGuardian."
