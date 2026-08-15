from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_SUFFIXES = {
    "Quant Guardian/Quant Guardian.exe",
    "Quant Guardian/VERSION",
    "scripts/install-app.ps1",
    "scripts/uninstall-app.ps1",
    "scripts/InstallSafety.psm1",
    "INSTALLATION.md",
    "LICENSE",
    "NOTICE",
    "THIRD-PARTY-NOTICES.md",
    "SBOM.cdx.json",
    "licenses/LGPL-3.0.txt",
    "licenses/GPL-2.0.txt",
    "licenses/GPL-3.0.txt",
    "licenses/PYTHON-LICENSE.txt",
    "licenses/PYINSTALLER-COPYING.txt",
    "licenses/QT-FOR-PYTHON-THIRD-PARTY-LICENSES.html",
}
FORBIDDEN_NAMES = {
    "quant-guardian.json",
    "monitor-heartbeat.json",
    "recovery_enabled",
    "xtminiqmt.exe",
    "xtitclient.exe",
    "quantclass.exe",
    "fuel.exe",
    "aqua.exe",
    "zeus.exe",
    "rocket.exe",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".jsonl",
    ".log",
    ".dmp",
    ".pem",
    ".key",
    ".pfx",
}
WINDOWS_USER_PATH = re.compile(rb"(?i)[a-z]:\\users\\[^\\\r\n\x00]+")
TEXT_SUFFIXES = {
    ".cfg",
    ".html",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def _relative_names(archive: zipfile.ZipFile) -> tuple[str, list[str]]:
    files = [name for name in archive.namelist() if not name.endswith("/")]
    if not files:
        raise ValueError("release ZIP is empty")
    roots = {PurePosixPath(name).parts[0] for name in files}
    if len(roots) != 1:
        raise ValueError("release ZIP must contain exactly one top-level directory")
    root = roots.pop()
    prefix = root + "/"
    return root, [name[len(prefix) :] for name in files]


def _forbidden_path_markers(extra_paths: tuple[str, ...]) -> set[bytes]:
    raw_paths = [
        os.environ.get("USERPROFILE", ""),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
        os.environ.get("TEMP", ""),
        os.environ.get("TMP", ""),
        *extra_paths,
    ]
    markers: set[bytes] = set()
    for raw_path in raw_paths:
        normalized = str(raw_path).strip().rstrip("\\/")
        if not normalized:
            continue
        for variant in {normalized.replace("/", "\\"), normalized.replace("\\", "/")}:
            markers.add(variant.casefold().encode("utf-8"))
    return markers


def validate_zip(path: Path, *, forbidden_paths: tuple[str, ...] = ()) -> None:
    forbidden_markers = _forbidden_path_markers(forbidden_paths)
    with zipfile.ZipFile(path) as archive:
        _root, names = _relative_names(archive)
        normalized = {name.replace("\\", "/") for name in names}
        missing = sorted(REQUIRED_SUFFIXES - normalized)
        if missing:
            raise ValueError(f"release ZIP is missing required files: {missing}")
        for name in normalized:
            pure = PurePosixPath(name)
            lower = name.casefold()
            if pure.name.casefold() in FORBIDDEN_NAMES:
                raise ValueError(f"release ZIP contains a forbidden file: {name}")
            if any(lower.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
                raise ValueError(f"release ZIP contains runtime/private data: {name}")
            if "/xtquant/" in f"/{lower}/":
                raise ValueError(f"release ZIP contains an XTQuant package: {name}")
        for info in archive.infolist():
            if info.is_dir():
                continue
            content = archive.read(info)
            content_folded = content.lower()
            if any(marker in content_folded for marker in forbidden_markers):
                raise ValueError(
                    f"release ZIP contains a forbidden local path: {info.filename}"
                )
            if PurePosixPath(info.filename).suffix.casefold() in TEXT_SUFFIXES and WINDOWS_USER_PATH.search(content):
                raise ValueError(f"release ZIP contains a Windows user path: {info.filename}")
        sbom_name = next(
            name for name in archive.namelist() if name.endswith("/SBOM.cdx.json")
        )
        sbom = json.loads(archive.read(sbom_name))
        if sbom.get("bomFormat") != "CycloneDX":
            raise ValueError("embedded SBOM is not CycloneDX")


def write_checksum(path: Path, destination: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    destination.write_text(f"{digest} *{path.name}\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("zip", type=Path)
    parser.add_argument("--checksum", type=Path)
    parser.add_argument("--forbidden-path", action="append", default=[])
    args = parser.parse_args()
    validate_zip(args.zip, forbidden_paths=tuple(args.forbidden_path))
    if args.checksum:
        write_checksum(args.zip, args.checksum)
    print(f"validated release: {args.zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
