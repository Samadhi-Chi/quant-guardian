from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.domain.models import LogSignal
from quant_guardian.monitors.log_monitor import QmtLogMonitor


class LogMonitorOffsetTests(unittest.TestCase):
    @staticmethod
    def append_at(path: Path, line: str, modified: float) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
        os.utime(path, (modified, modified))

    def test_file_switch_does_not_replay_historical_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disconnect = root / "transport.log"
            success = root / "account.log"
            disconnect.write_text("proxy disconnect: End of file\n", encoding="utf-8")
            success.write_text("account initializer login success\n", encoding="utf-8")
            base = time.time()
            os.utime(disconnect, (base, base))
            os.utime(success, (base + 1, base + 1))
            monitor = QmtLogMonitor(root, stale_seconds=3600)

            baseline = monitor.observe()
            self.assertEqual(baseline.signal, LogSignal.NEUTRAL)

            self.append_at(disconnect, "heartbeat\n", base + 2)
            neutral = monitor.observe()
            self.assertEqual(neutral.signal, LogSignal.NEUTRAL)

            self.append_at(success, "account initializer login success\n", base + 3)
            positive = monitor.observe()
            self.assertEqual(positive.signal, LogSignal.POSITIVE)

            self.append_at(disconnect, "heartbeat\n", base + 4)
            retained = monitor.observe()
            self.assertEqual(retained.signal, LogSignal.POSITIVE)

    def test_new_disconnect_and_later_success_are_observed_across_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = root / "transport.log"
            account = root / "account.log"
            transport.write_text("transport ready\n", encoding="utf-8")
            account.write_text("account ready\n", encoding="utf-8")
            base = time.time()
            os.utime(transport, (base, base))
            os.utime(account, (base + 1, base + 1))
            monitor = QmtLogMonitor(root, stale_seconds=3600)
            self.assertEqual(monitor.observe().signal, LogSignal.NEUTRAL)

            self.append_at(transport, "broker proxy disconnect: End of file\n", base + 2)
            self.assertEqual(
                monitor.observe().signal,
                LogSignal.EXPLICIT_DISCONNECT,
            )

            self.append_at(account, "push accountdetail success\n", base + 3)
            self.assertEqual(monitor.observe().signal, LogSignal.POSITIVE)

    def test_locked_log_degrades_supporting_evidence_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "account.log"
            log.write_text("account ready\n", encoding="utf-8")
            monitor = QmtLogMonitor(root, stale_seconds=3600)
            self.assertEqual(monitor.observe().signal, LogSignal.NEUTRAL)

            with patch.object(
                monitor,
                "_read_new_lines",
                side_effect=PermissionError("synthetic file lock"),
            ):
                observation = monitor.observe()

            self.assertEqual(observation.signal, LogSignal.UNAVAILABLE)
            self.assertIn("temporarily unreadable", observation.reason)
            self.assertIn("PermissionError", observation.reason)


if __name__ == "__main__":
    unittest.main()
