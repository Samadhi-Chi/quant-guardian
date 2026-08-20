from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from quant_guardian import __version__
from scripts.check_sarif import actionable_results
from scripts.check_version import release_tag, validate_project_metadata
from scripts.generate_sbom import build_sbom
from scripts.validate_release import REQUIRED_SUFFIXES, validate_zip

SYNTHETIC_TELEGRAM_TOKEN = ("123456789:" + "AAA" + ("b" * 32)).encode()


class ReleaseContractTests(unittest.TestCase):
    def test_version_has_single_source_and_matches_public_tag(self) -> None:
        validate_project_metadata()
        self.assertEqual(__version__, "0.4.0b1")
        self.assertEqual(release_tag(__version__), "v0.4.0-beta.1")

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
        ci_python_argument = (
            "-Python (Get-Command python -CommandType Application "
            "| Select-Object -First 1 -ExpandProperty Source)"
        )
        self.assertEqual(workflow.count(ci_python_argument), 2)

    def test_gitleaks_pr_scan_uses_read_only_token_without_comments(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        workflow = (project_root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pull-requests: read", workflow)
        self.assertIn(
            "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
            workflow,
        )
        self.assertIn('GITLEAKS_ENABLE_COMMENTS: "false"', workflow)

    def test_sarif_gate_accepts_empty_results_and_rejects_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codeql.sarif"
            path.write_text(
                json.dumps({"version": "2.1.0", "runs": [{"results": []}]}),
                encoding="utf-8",
            )
            self.assertEqual(actionable_results(path), [])
            path.write_text(
                json.dumps(
                    {
                        "version": "2.1.0",
                        "runs": [{"results": [{"ruleId": "py/example"}]}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                [item["ruleId"] for item in actionable_results(path)],
                ["py/example"],
            )

    def make_zip(self, path: Path, *, extra: dict[str, bytes] | None = None) -> None:
        prefix = "Quant-Guardian-v0.4.0-beta.1-windows-x64/"
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
            "runtime/config/messaging.json": b"{}",
            "runtime/secrets/messaging-secrets.json": b"{}",
        }
        with tempfile.TemporaryDirectory() as directory:
            for index, (name, content) in enumerate(cases.items()):
                path = Path(directory) / f"release-{index}.zip"
                self.make_zip(path, extra={name: content})
                with self.assertRaises(ValueError, msg=name):
                    validate_zip(path)

    def test_release_validator_rejects_telegram_bot_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.zip"
            self.make_zip(
                path,
                extra={"INSTALLATION.md": SYNTHETIC_TELEGRAM_TOKEN},
            )
            with self.assertRaisesRegex(ValueError, "Telegram bot token"):
                validate_zip(path)

    def test_release_validator_ignores_token_shaped_random_binary_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.zip"
            self.make_zip(
                path,
                extra={
                    "Quant Guardian/_internal/PySide6/Qt6Pdf.dll": (
                        b"binary\x00" + SYNTHETIC_TELEGRAM_TOKEN
                    )
                },
            )
            validate_zip(path)

    def test_release_validator_scans_packaged_python_source_for_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.zip"
            self.make_zip(
                path,
                extra={
                    "Quant Guardian/probe_runtime/example.py": (
                        b"TOKEN = '" + SYNTHETIC_TELEGRAM_TOKEN + b"'"
                    )
                },
            )
            with self.assertRaisesRegex(ValueError, "Telegram bot token"):
                validate_zip(path)

    def test_sbom_lists_gateway_runtime_dependencies(self) -> None:
        components = {item["name"]: item for item in build_sbom()["components"]}
        self.assertEqual(components["qrcode"]["licenses"][0]["expression"], "BSD-3-Clause")
        self.assertIn("colorama", components)

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
