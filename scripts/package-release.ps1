[CmdletBinding()]
param(
    [string]$Version = "0.3.0b1",
    [string]$ReleaseTag = "v0.3.0-beta.1",
    [string]$Python = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$LocalPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not $Python) {
    if (Test-Path -LiteralPath $LocalPython -PathType Leaf) {
        $Python = $LocalPython
    } else {
        $PythonCommand = @(Get-Command python -CommandType Application -ErrorAction SilentlyContinue)[0]
        if ($PythonCommand) {
            $Python = [string]$PythonCommand.Source
        }
    }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python 3.11-3.14 was not found. Run scripts\bootstrap.ps1 -Dev or pass -Python."
}

Push-Location $ProjectRoot
try {
    $ActualVersion = (& $Python ".\scripts\check_version.py" --version).Trim()
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($ActualVersion -ne $Version) {
        throw "Package version mismatch. Expected $Version, found $ActualVersion"
    }
    & $Python ".\scripts\check_version.py" --tag $ReleaseTag | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if (-not $SkipBuild) {
        & ".\scripts\build.ps1" -Python $Python
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    $BuiltApplication = Join-Path $ProjectRoot "dist\Quant Guardian"
    $BuiltExecutable = Join-Path $BuiltApplication "Quant Guardian.exe"
    if (-not (Test-Path -LiteralPath $BuiltExecutable -PathType Leaf)) {
        throw "Built application not found: $BuiltExecutable"
    }

    $AssetStem = "Quant-Guardian-$ReleaseTag"
    $StageParent = Join-Path $ProjectRoot "release-staging"
    $Stage = Join-Path $StageParent "$AssetStem-windows-x64"
    $AssetDirectory = Join-Path $ProjectRoot "release-assets"
    foreach ($Target in @($StageParent, $AssetDirectory)) {
        $ResolvedTarget = [IO.Path]::GetFullPath($Target)
        if (-not $ResolvedTarget.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean an unexpected release path: $ResolvedTarget"
        }
        if (Test-Path -LiteralPath $ResolvedTarget) {
            Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
        }
        New-Item -ItemType Directory -Path $ResolvedTarget -Force | Out-Null
    }
    New-Item -ItemType Directory -Path $Stage -Force | Out-Null
    Copy-Item -LiteralPath $BuiltApplication -Destination (Join-Path $Stage "Quant Guardian") -Recurse
    New-Item -ItemType Directory -Path (Join-Path $Stage "scripts") -Force | Out-Null
    foreach ($Script in @("install-app.ps1", "uninstall-app.ps1", "InstallSafety.psm1")) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $Script) -Destination (Join-Path $Stage "scripts\$Script")
    }
    Copy-Item -LiteralPath ".\docs\INSTALLATION.md" -Destination (Join-Path $Stage "INSTALLATION.md")
    foreach ($File in @("LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md")) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $File) -Destination (Join-Path $Stage $File)
    }
    Copy-Item -LiteralPath ".\licenses" -Destination (Join-Path $Stage "licenses") -Recurse

    $SbomAsset = Join-Path $AssetDirectory "$AssetStem-SBOM.cdx.json"
    & $Python ".\scripts\generate_sbom.py" $SbomAsset
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Copy-Item -LiteralPath $SbomAsset -Destination (Join-Path $Stage "SBOM.cdx.json")

    $ZipAsset = Join-Path $AssetDirectory "$AssetStem-windows-x64.zip"
    Compress-Archive -LiteralPath $Stage -DestinationPath $ZipAsset -CompressionLevel Optimal
    $ChecksumAsset = Join-Path $AssetDirectory "$AssetStem-SHA256SUMS.txt"
    & $Python ".\scripts\validate_release.py" $ZipAsset --checksum $ChecksumAsset
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Get-ChildItem -LiteralPath $AssetDirectory -File |
        Sort-Object Name |
        Select-Object Name, Length
} finally {
    Pop-Location
}
