Set-StrictMode -Version Latest

$script:QuantGuardianProductId = "com.samadhi-chi.quant-guardian"
$script:QuantGuardianMarkerName = ".quant-guardian-install.json"
$script:QuantGuardianExecutableName = "Quant Guardian.exe"

function Get-QgCanonicalPath {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Path must not be empty."
    }
    return [IO.Path]::GetFullPath($Path)
}

function Test-QgSamePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )

    $leftPath = (Get-QgCanonicalPath $Left).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $rightPath = (Get-QgCanonicalPath $Right).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    return [string]::Equals(
        $leftPath,
        $rightPath,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-QgSafeDestination {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Destination,
        [string]$ProjectRoot = ""
    )

    $resolved = Get-QgCanonicalPath $Destination
    $driveRoot = [IO.Path]::GetPathRoot($resolved)
    if ($driveRoot -and (Test-QgSamePath $resolved $driveRoot)) {
        throw "Refusing to use a drive root as the application destination: $resolved"
    }

    $protected = [ordered]@{}
    if ($env:USERPROFILE) {
        $protected["user profile"] = $env:USERPROFILE
    }
    if ($env:LOCALAPPDATA) {
        $protected["LocalAppData root"] = $env:LOCALAPPDATA
        $protected["Programs root"] = Join-Path $env:LOCALAPPDATA "Programs"
    }
    if ($ProjectRoot) {
        $protected["repository root"] = $ProjectRoot
    }

    foreach ($entry in $protected.GetEnumerator()) {
        if (Test-QgSamePath $resolved ([string]$entry.Value)) {
            throw "Refusing to use the $($entry.Key) as the application destination: $resolved"
        }
    }
    return $resolved
}

function Write-QgInstallMarker {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Directory,
        [Parameter(Mandatory)][string]$InstallRoot,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$ExecutableSha256
    )

    $document = [ordered]@{
        product_id = $script:QuantGuardianProductId
        install_root = Get-QgCanonicalPath $InstallRoot
        version = $Version
        executable_sha256 = $ExecutableSha256.ToUpperInvariant()
        installed_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $marker = Join-Path (Get-QgCanonicalPath $Directory) $script:QuantGuardianMarkerName
    $document | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding utf8
    return $marker
}

function Assert-QgValidInstallation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Destination,
        [string]$ProjectRoot = ""
    )

    $resolved = Assert-QgSafeDestination -Destination $Destination -ProjectRoot $ProjectRoot
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Quant Guardian is not installed at: $resolved"
    }
    $markerPath = Join-Path $resolved $script:QuantGuardianMarkerName
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Refusing to remove an unmarked directory: $resolved"
    }
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        throw "Refusing to remove a directory with an invalid install marker: $resolved"
    }
    if ($marker.product_id -ne $script:QuantGuardianProductId) {
        throw "Refusing to remove a directory with an unexpected product ID: $resolved"
    }
    if (-not (Test-QgSamePath ([string]$marker.install_root) $resolved)) {
        throw "Refusing to remove a directory whose install marker names another path: $resolved"
    }
    if ([string]::IsNullOrWhiteSpace([string]$marker.version)) {
        throw "Refusing to remove a directory whose install marker has no version: $resolved"
    }
    $executable = Join-Path $resolved $script:QuantGuardianExecutableName
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Refusing to remove a directory without the expected executable: $resolved"
    }
    $actualHash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash
    if (-not [string]::Equals(
        $actualHash,
        [string]$marker.executable_sha256,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove a directory whose executable does not match its install marker: $resolved"
    }
    return $resolved
}

Export-ModuleMember -Function @(
    "Assert-QgSafeDestination",
    "Assert-QgValidInstallation",
    "Get-QgCanonicalPath",
    "Test-QgSamePath",
    "Write-QgInstallMarker"
)
