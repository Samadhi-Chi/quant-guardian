from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import AppConfig
from quant_guardian.diagnostics.exporter import DiagnosticExporter


class DiagnosticExporterTests(unittest.TestCase):
    def test_export_contains_no_account_secret_username_or_product_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            logs = runtime / "logs"
            logs.mkdir(parents=True)
            qmt_root = root / "Private QMT"
            quantclass_root = root / "Private Quantclass"
            qmt_root.mkdir()
            quantclass_root.mkdir()
            config = AppConfig()
            config.qmt.launcher = str(qmt_root / "bin.x64" / "XtItClient.exe")
            config.qmt.working_directory = str(qmt_root / "config")
            config.qmt.userdata_directory = str(qmt_root / "userdata_mini")
            config.qmt.log_directory = str(qmt_root / "userdata_mini" / "log")
            config.trade_system.client_executable = str(
                quantclass_root / "quantclass.exe"
            )
            config.trade_system.quantclass_config = str(
                quantclass_root / "config.json"
            )
            config.trade_system.data_root = str(quantclass_root / "data")
            config.probe.account_id_protected = "123456789012"
            username = os.environ.get("USERNAME", "")
            (logs / "guardian-20260815.jsonl").write_text(
                json.dumps(
                    {
                        "account": "123456789012",
                        "token": "super-secret-token",
                        "qmt": str(qmt_root / "userdata_mini"),
                        "quantclass": str(quantclass_root / "data"),
                        "home": str(Path.home()),
                    }
                ),
                encoding="utf-8",
            )
            destination = root / "diagnostics.zip"
            with patch.dict(
                os.environ,
                {
                    "TEMP": str(root),
                    "TMP": str(root),
                },
                clear=False,
            ):
                DiagnosticExporter(runtime).export(destination, config)

            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["logs/guardian-20260815.jsonl", "metadata.json"],
                )
                content = "\n".join(
                    archive.read(name).decode("utf-8")
                    for name in archive.namelist()
                )
            self.assertNotIn("123456789012", content)
            self.assertNotIn("super-secret-token", content)
            self.assertNotIn(str(qmt_root), content)
            self.assertNotIn(str(quantclass_root), content)
            if username:
                self.assertNotIn(username, content)
            self.assertIn("<QMT_ROOT>", content)
            self.assertIn("<QUANTCLASS_ROOT>", content)


if __name__ == "__main__":
    unittest.main()
