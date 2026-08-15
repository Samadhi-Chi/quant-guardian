from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from quant_guardian import __version__
from scripts.check_version import release_tag, validate_project_metadata
from scripts.validate_release import REQUIRED_SUFFIXES, validate_zip


class ReleaseContractTests(unittest.TestCase):
    def test_version_has_single_source_and_matches_public_tag(self) -> None:
        validate_project_metadata()
        self.assertEqual(__version__, "0.3.0b1")
        self.assertEqual(release_tag(__version__), "v0.3.0-beta.1")

    def test_build_scripts_support_ci_python_without_repository_venv(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        build_script = (project_root / "scripts" / "build.ps1").read_text(
            encoding="utf-8"
        )
        package_script = (project_root / "scripts" / "package-release.ps1").read_text(
            encoding="utf-8"
        )
        workflow = (project_root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Get-Command python -CommandType Application", build_script)
        self.assertIn("[string]$Python", package_script)
        self.assertIn("-Python (Get-Command python -CommandType Application).Source", workflow)

    def make_zip(self, path: Path, *, extra: dict[str, bytes] | None = None) -> None:
        prefix = "Quant-Guardian-v0.3.0-beta.1-windows-x64/"
        remaining = dict(extra or {})
        with zipfile.ZipFile(path, "w") as archive:
            for name in REQUIRED_SUFFIXES:
                content = remaining.pop(name, b"x")
                if name == "SBOM.cdx.json" and name not in (extra or {}):
                    content = json.dumps(
                        {"bomFormat": "CycloneDX", "specVersion": "1.6"}
                    ).encode()
                archive.writestr(prefix + name, content)
            for name, content in remaining.items():
                archive.writestr(prefix + name, content)

    def test_release_validator_accepts_required_public_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.zip"
            self.make_zip(path)
            validate_zip(path)

    def test_release_validator_rejects_runtime_log_and_external_binary(self) -> None:
        cases = {
            "runtime/guardian.jsonl": b"{}",
            "Quant Guardian/quantclass.exe": b"external",
            "runtime/config/quant-guardian.json": b"{}",
            "external/xtquant/__init__.py": b"external",
        }
        with tempfile.TemporaryDirectory() as directory:
            for index, (name, content) in enumerate(cases.items()):
                path = Path(directory) / f"release-{index}.zip"
                self.make_zip(path, extra={name: content})
                with self.assertRaises(ValueError, msg=name):
                    validate_zip(path)

    def test_release_validator_rejects_windows_user_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.zip"
            self.make_zip(
                path,
                extra={"metadata.txt": rb"C:\Users\private-user\QMT\config"},
            )
            with self.assertRaisesRegex(ValueError, "Windows user path"):
                validate_zip(path)

    def test_release_validator_allows_upstream_qt_binary_build_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.zip"
            self.make_zip(
                path,
                extra={"Quant Guardian/_internal/PySide6/Qt6Network.dll": rb"C:\Users\qt"},
            )
            validate_zip(path)

    def test_release_validator_rejects_explicit_local_path_in_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.zip"
            local_root = r"C:\Users\private-user"
            self.make_zip(
                path,
                extra={"Quant Guardian/Quant Guardian.exe": (local_root + r"\build").encode()},
            )
            with self.assertRaisesRegex(ValueError, "forbidden local path"):
                validate_zip(path, forbidden_paths=(local_root,))


if __name__ == "__main__":
    unittest.main()
