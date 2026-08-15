from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import AppConfig, load_config, save_config
from quant_guardian.safety import SENTINEL_CONTENT, SafetyGate


class ConfigAndSafetyTests(unittest.TestCase):
    def test_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig()
            config.mode = "observe"
            save_config(config, path)
            loaded = load_config(path)
            self.assertEqual(loaded.mode, "observe")
            self.assertEqual(loaded.qmt.process_names[0], "XtMiniQmt.exe")
            self.assertEqual(loaded.trade_system.selection_engine, "zeus")

    def test_selection_engine_must_be_aqua_or_zeus(self) -> None:
        config = AppConfig()
        config.trade_system.selection_engine = "other"
        self.assertIn(
            "trade_system.selection_engine must be 'aqua' or 'zeus'",
            config.validate(),
        )

    def test_private_probe_runtime_is_auto_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "LocalAppData"
            python = local_app_data / "QuantGuardian" / "Python311" / "python.exe"
            package = local_app_data / "QuantGuardian" / "XtQuant" / "xtquant"
            python.parent.mkdir(parents=True)
            package.mkdir(parents=True)
            python.touch()
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                config = load_config(Path(directory) / "missing.json")
            self.assertEqual(config.probe.python_executable, str(python))
            self.assertEqual(config.probe.xtquant_parent, str(package.parent))

    def test_v1_config_is_backed_up_and_migrated_to_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quant-guardian.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "observe",
                        "thresholds": {
                            "poll_interval_seconds": 7.5,
                            "failure_threshold": 3,
                            "startup_grace_seconds": 90,
                            "verify_successes": 3,
                        },
                        "rocket": {
                            "enabled": True,
                            "process_names": ["rocket.exe"],
                            "log_directory": r"C:\data\rocket\logs",
                        },
                        "trading": {"holidays": ["2026-08-11"]},
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_config(path)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded.schema_version, 2)
            self.assertEqual(loaded.monitoring.active_interval_seconds, 7.5)
            self.assertIn("2026-08-11", loaded.trading.manual_closed_dates)
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(len(list(path.parent.glob("*.v1-backup-*.json"))), 1)

    def test_quantclass_data_root_is_auto_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root = root / "stock-data"
            data_root.mkdir()
            quantclass = root / "quantclass.json"
            quantclass.write_text(
                json.dumps({"settings": {"all_data_path": str(data_root)}}),
                encoding="utf-8",
            )
            config_path = root / "guardian.json"
            config = AppConfig()
            config.trade_system.quantclass_config = str(quantclass)
            save_config(config, config_path)
            loaded = load_config(config_path)
            self.assertEqual(loaded.trade_system.data_root, str(data_root))

    def test_recovery_requires_mode_and_exact_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            config = AppConfig()
            config.mode = "recover"
            gate = SafetyGate(config, sentinel)
            self.assertFalse(gate.status().live_actions_allowed)
            sentinel.write_text("wrong\n", encoding="utf-8")
            self.assertFalse(gate.status().live_actions_allowed)
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            self.assertTrue(gate.status().live_actions_allowed)

    def test_temporary_recovery_authorization_expires_at_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            config = AppConfig()
            config.mode = "recover"
            now = datetime(2026, 8, 13, 8, 59, tzinfo=UTC)
            config.recovery.automatic_recovery_until = (
                now + timedelta(minutes=1)
            ).isoformat()
            gate = SafetyGate(config, sentinel)
            self.assertTrue(gate.status(now).live_actions_allowed)
            expired = gate.status(now + timedelta(minutes=1))
            self.assertFalse(expired.live_actions_allowed)
            self.assertIn("到期", expired.reason)


if __name__ == "__main__":
    unittest.main()
