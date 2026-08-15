from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import uuid
from datetime import UTC, datetime
from pathlib import Path

from quant_guardian import __version__

LICENSES = {
    "quant-guardian": "Apache-2.0",
    "PySide6": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "PySide6-Addons": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "PySide6-Essentials": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "shiboken6": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
    "psutil": "BSD-3-Clause",
    "tzdata": "Apache-2.0",
}
RUNTIME_PACKAGES = (
    "PySide6",
    "PySide6-Addons",
    "PySide6-Essentials",
    "shiboken6",
    "psutil",
    "tzdata",
)


def component(name: str, version: str, license_id: str) -> dict[str, object]:
    normalized = name.casefold().replace("_", "-")
    return {
        "type": "library",
        "bom-ref": f"pkg:pypi/{normalized}@{version}",
        "name": name,
        "version": version,
        "purl": f"pkg:pypi/{normalized}@{version}",
        "licenses": [{"expression": license_id}],
    }


def build_sbom() -> dict[str, object]:
    components: list[dict[str, object]] = []
    dependencies: list[str] = []
    for name in RUNTIME_PACKAGES:
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            if name == "tzdata":
                continue
            raise
        item = component(name, version, LICENSES[name])
        components.append(item)
        dependencies.append(str(item["bom-ref"]))
    app_ref = f"pkg:pypi/quant-guardian@{__version__}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "Quant Guardian SBOM generator",
                        "version": __version__,
                    }
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": app_ref,
                "name": "quant-guardian",
                "version": __version__,
                "purl": app_ref,
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            },
            "properties": [
                {"name": "quant-guardian:python", "value": platform.python_version()},
                {"name": "quant-guardian:platform", "value": platform.platform()},
            ],
        },
        "components": components,
        "dependencies": [{"ref": app_ref, "dependsOn": dependencies}],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_sbom(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
