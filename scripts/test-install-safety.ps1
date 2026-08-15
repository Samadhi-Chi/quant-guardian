[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Import-Module (Join-Path $PSScriptRoot "InstallSafety.psm1") -Force
$Failures = [Collections.Generic.List[string]]::new()

function Expect-Blocked {
    param([string]$Name, [scriptblock]$Action)
    try {
        & $Action
        $Failures.Add("$Name was not blocked")
    } catch {
        Write-Host "PASS blocked: $Name"
    }
}

$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("quant-guardian-uninstall-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
try {
    $driveRoot = [IO.Path]::GetPathRoot($ProjectRoot)
    Expect-Blocked "drive root" { Assert-QgSafeDestination $driveRoot $ProjectRoot }
    if ($env:USERPROFILE) {
        Expect-Blocked "user profile" { Assert-QgSafeDestination $env:USERPROFILE $ProjectRoot }
    }
    if ($env:LOCALAPPDATA) {
        Expect-Blocked "LocalAppData root" { Assert-QgSafeDestination $env:LOCALAPPDATA $ProjectRoot }
        Expect-Blocked "Programs root" {
            Assert-QgSafeDestination (Join-Path $env:LOCALAPPDATA "Programs") $ProjectRoot
        }
    }
    Expect-Blocked "repository root" { Assert-QgSafeDestination $ProjectRoot $ProjectRoot }

    $Unmarked = Join-Path $TempRoot "unmarked"
    New-Item -ItemType Directory -Path $Unmarked | Out-Null
    New-Item -ItemType File -Path (Join-Path $Unmarked "Quant Guardian.exe") | Out-Null
    Expect-Blocked "unmarked custom destination" {
        Assert-QgValidInstallation $Unmarked $ProjectRoot
    }

    $Valid = Join-Path $TempRoot "valid-custom"
    New-Item -ItemType Directory -Path $Valid | Out-Null
    $Executable = Join-Path $Valid "Quant Guardian.exe"
    Set-Content -LiteralPath $Executable -Value "test executable" -Encoding ascii
    $Hash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
    Write-QgInstallMarker $Valid $Valid "0.3.0b1" $Hash | Out-Null
    $Resolved = Assert-QgValidInstallation $Valid $ProjectRoot
    if (-not (Test-QgSamePath $Resolved $Valid)) {
        $Failures.Add("valid custom destination did not round-trip")
    } else {
        Write-Host "PASS accepted: marked custom destination"
    }

    & (Join-Path $PSScriptRoot "uninstall-app.ps1") `
        -Destination $Valid `
        -KeepRegistration `
        -WhatIf `
        -Confirm:$false
    if (-not (Test-Path -LiteralPath $Valid -PathType Container)) {
        $Failures.Add("-WhatIf removed the installation")
    } else {
        Write-Host "PASS preserved: -WhatIf"
    }

    & (Join-Path $PSScriptRoot "uninstall-app.ps1") `
        -Destination $Valid `
        -KeepRegistration `
        -Confirm:$false
    if (Test-Path -LiteralPath $Valid) {
        $Failures.Add("verified custom installation was not removed")
    } else {
        Write-Host "PASS removed: verified custom installation"
    }

    $Tampered = Join-Path $TempRoot "tampered"
    New-Item -ItemType Directory -Path $Tampered | Out-Null
    $TamperedExecutable = Join-Path $Tampered "Quant Guardian.exe"
    Set-Content -LiteralPath $TamperedExecutable -Value "before" -Encoding ascii
    $OriginalHash = (Get-FileHash -LiteralPath $TamperedExecutable -Algorithm SHA256).Hash
    Write-QgInstallMarker $Tampered $Tampered "0.3.0b1" $OriginalHash | Out-Null
    Set-Content -LiteralPath $TamperedExecutable -Value "after" -Encoding ascii
    Expect-Blocked "tampered executable" {
        Assert-QgValidInstallation $Tampered $ProjectRoot
    }
} finally {
    if (Test-Path -LiteralPath $TempRoot) {
        $resolvedTemp = [IO.Path]::GetFullPath($TempRoot)
        $systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if (-not $resolvedTemp.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean unexpected test path: $resolvedTemp"
        }
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}

if ($Failures.Count) {
    $Failures | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host "Install and uninstall safety tests passed." -ForegroundColor Green
