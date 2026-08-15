from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_guardian import __version__  # noqa: E402

VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?P<kind>a|b|rc)(?P<number>0|[1-9]\d*))?$"
)


def release_tag(version: str) -> str:
    match = VERSION_RE.fullmatch(version)
    if not match:
        raise ValueError(f"unsupported package version: {version}")
    base = f"v{match['major']}.{match['minor']}.{match['patch']}"
    if not match["kind"]:
        return base
    labels = {"a": "alpha", "b": "beta", "rc": "rc"}
    return f"{base}-{labels[match['kind']]}.{match['number']}"


def validate_project_metadata() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    if "version" in project:
        raise ValueError("pyproject.toml must not contain a second static version")
    if "version" not in project.get("dynamic", []):
        raise ValueError("pyproject.toml must declare version as dynamic")
    source = document["tool"]["setuptools"]["dynamic"]["version"]
    if source.get("attr") != "quant_guardian.__version__":
        raise ValueError("setuptools version must come from quant_guardian.__version__")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    validate_project_metadata()
    expected = release_tag(__version__)
    if args.tag and args.tag != expected:
        raise SystemExit(
            f"release tag mismatch: package {__version__} requires {expected}, got {args.tag}"
        )
    print(__version__ if args.version else expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
