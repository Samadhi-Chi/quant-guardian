[CmdletBinding()]
param(
    [string]$TargetDirectory = "",
    [switch]$SkipXtQuant
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$DownloadDirectory = Join-Path $ProjectRoot ".downloads"
$Installer = Join-Path $DownloadDirectory "python-3.11.9-amd64.exe"
$ExpectedSha256 = "5EE42C4EEE1E6B4464BB23722F90B45303F79442DF63083F05322F1785F5FDDE"
if (-not $TargetDirectory) {
    $TargetDirectory = Join-Path $env:LOCALAPPDATA "QuantGuardian\Python311"
}
$TargetDirectory = [IO.Path]::GetFullPath($TargetDirectory)
$Python = Join-Path $TargetDirectory "python.exe"

if (Test-Path -LiteralPath $Python -PathType Leaf) {
    & $Python -c "import struct, sys; assert sys.version_info[:2] == (3, 11); assert struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) {
        throw "An incompatible Python exists at $Python"
    }
    Write-Host "Private Python 3.11 x64 is already ready: $Python" -ForegroundColor Green
} else {
    [IO.Directory]::CreateDirectory($DownloadDirectory) | Out-Null
    # Python 3.11.9 is the final 3.11 release with an official Windows installer.
    # Newer 3.11 security releases are source-only. This runtime is isolated and
    # exists only for the cp311 XTQuant native probe.
    if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $Installer -UseBasicParsing
    }

    $ActualSha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash
    if ($ActualSha256 -ne $ExpectedSha256) {
        throw "Python installer SHA-256 mismatch. Expected $ExpectedSha256, got $ActualSha256"
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Installer
    if ($Signature.Status -ne [Management.Automation.SignatureStatus]::Valid) {
        throw "Python installer signature is invalid: $($Signature.Status)"
    }
    if ($Signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        throw "Unexpected Python installer signer: $($Signature.SignerCertificate.Subject)"
    }

    $Arguments = @(
        "/quiet",
        "InstallAllUsers=0",
        ("TargetDir=" + $TargetDirectory),
        "PrependPath=0",
        "AppendPath=0",
        "Include_launcher=0",
        "Include_test=0",
        "Include_doc=0",
        "Include_pip=1",
        "Include_tcltk=0",
        "Shortcuts=0"
    )
    $Process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -notin @(0, 3010)) {
        throw "Private Python installation failed with exit code $($Process.ExitCode)"
    }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Private Python executable was not created: $Python"
    }
    & $Python -c "import struct, sys; assert sys.version_info[:2] == (3, 11); assert struct.calcsize('P') == 8; print(sys.version)"
    if ($LASTEXITCODE -ne 0) { throw "Private Python verification failed" }
    Write-Host "Private Python installed without changing PATH: $Python" -ForegroundColor Green
}

if (-not $SkipXtQuant) {
    & (Join-Path $PSScriptRoot "install-xtquant.ps1") -PythonExe $Python
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
