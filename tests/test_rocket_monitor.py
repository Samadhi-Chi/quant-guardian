from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import RocketConfig
from quant_guardian.monitors.rocket_monitor import RocketMonitor


class RocketMonitorTests(unittest.TestCase):
    def test_explicit_business_success_controls_heartbeat_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            heartbeat = now - timedelta(seconds=30)
            path = root / f"{now:%Y-%m-%d}_日志.log"
            path.write_text(
                f"INFO:root:{heartbeat:%H:%M:%S} --> "
                "ex_api.refresh_entrusts运行成功\n",
                encoding="utf-8",
            )
            config = RocketConfig(
                log_directory=str(root),
                business_heartbeat_stale_seconds=120,
            )
            monitor = RocketMonitor(config)
            with patch.object(monitor, "_process_active", return_value=True):
                fresh = monitor.observe(now)
                stale = monitor.observe(now + timedelta(seconds=180))
            self.assertTrue(fresh.business_healthy)
            self.assertTrue(fresh.business_health_known)
            self.assertEqual(fresh.heartbeat_source, "explicit_business_success")
            self.assertAlmostEqual(fresh.business_age_seconds or 0, 30, delta=1)
            self.assertFalse(stale.business_healthy)
            self.assertGreater(stale.business_age_seconds or 0, 120)
            self.assertIn("心跳已过期", stale.reason)

    def test_fresh_log_is_conservative_fallback_for_older_rocket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            path = root / f"{now:%Y-%m-%d}_日志.log"
            path.write_text("INFO:root:Rocket is running\n", encoding="utf-8")
            os.utime(path, (now.timestamp(), now.timestamp()))
            monitor = RocketMonitor(
                RocketConfig(
                    log_directory=str(root),
                    business_heartbeat_stale_seconds=120,
                )
            )
            with patch.object(monitor, "_process_active", return_value=True):
                observation = monitor.observe(now)
            self.assertTrue(observation.business_healthy)
            self.assertTrue(observation.business_health_known)
            self.assertEqual(
                observation.heartbeat_source,
                "log_freshness_fallback",
            )


if __name__ == "__main__":
    unittest.main()
