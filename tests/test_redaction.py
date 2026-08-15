from __future__ import annotations

import unittest
from unittest.mock import patch

from quant_guardian.config import AppConfig
from quant_guardian.diagnostics.redaction import PathRedactor, redact, redact_text


class RedactionTests(unittest.TestCase):
    def test_account_and_token_are_redacted(self) -> None:
        value = redact_text("account=123456789012 token=abcdef")
        self.assertNotIn("123456789012", value)
        self.assertNotIn("abcdef", value)
        self.assertIn("12", value)

    def test_sensitive_values_are_removed_recursively(self) -> None:
        value = redact(
            {
                "total_asset": 123.45,
                "nested": {"password": "secret", "ok": True},
                "account_id": "1234567890",
            }
        )
        self.assertEqual(value["total_asset"], "<redacted>")
        self.assertEqual(value["nested"]["password"], "<redacted>")
        self.assertEqual(value["account_id"], "********90")

    def test_path_redactor_masks_configured_roots_and_environment(self) -> None:
        config = AppConfig()
        config.qmt.launcher = r"C:\Trading\QMT\bin.x64\XtItClient.exe"
        config.qmt.working_directory = r"C:\Trading\QMT\config"
        config.qmt.userdata_directory = r"C:\Trading\QMT\userdata_mini"
        config.qmt.log_directory = r"C:\Trading\QMT\userdata_mini\log"
        config.trade_system.client_executable = r"D:\Apps\Quantclass\quantclass.exe"
        config.trade_system.quantclass_config = r"D:\Apps\Quantclass\config.json"
        config.trade_system.data_root = r"D:\Apps\Quantclass\data"
        with patch.dict(
            "os.environ",
            {
                "USERPROFILE": r"C:\Users\private-user",
                "LOCALAPPDATA": r"C:\Users\private-user\AppData\Local",
                "APPDATA": r"C:\Users\private-user\AppData\Roaming",
                "TEMP": r"C:\Users\private-user\AppData\Local\Temp",
            },
            clear=False,
        ):
            sanitizer = PathRedactor.from_config(config)
        value = sanitizer.redact_text(
            r"C:\Trading\QMT\userdata_mini\log\x.log "
            r"D:\Apps\Quantclass\data\real_trading "
            r"C:\Users\private-user\AppData\Local\QuantGuardian"
        )
        self.assertIn(r"<QMT_ROOT>\userdata_mini\log\x.log", value)
        self.assertIn(r"<QUANTCLASS_ROOT>\data\real_trading", value)
        self.assertIn(r"%LOCALAPPDATA%\QuantGuardian", value)
        self.assertNotIn("private-user", value)


if __name__ == "__main__":
    unittest.main()
