from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import ProbeConfig, QmtConfig
from quant_guardian.probe.protocol import ProbeRequest, ProbeResponse
from quant_guardian.probe.supervisor import ProbeSupervisor


class ProbeProcessEncodingTests(unittest.TestCase):
    def test_recovery_reset_rotates_xtquant_session_identity(self) -> None:
        supervisor = ProbeSupervisor(ProbeConfig(session_id=1234), QmtConfig())
        original = supervisor.probe_config.session_id
        supervisor._consecutive_timeouts = 2
        supervisor.reset_after_recovery()
        self.assertNotEqual(supervisor.probe_config.session_id, original)
        self.assertEqual(supervisor._consecutive_timeouts, 0)

    def test_cold_worker_has_separate_startup_budget(self) -> None:
        supervisor = ProbeSupervisor(
            ProbeConfig(timeout_seconds=2),
            QmtConfig(),
        )
        self.assertEqual(supervisor._response_timeout(cold_start=True), 5.0)
        self.assertEqual(supervisor._response_timeout(cold_start=False), 2.0)

    def test_spawned_worker_forces_utf8_for_chinese_windows_paths(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        broker_name = "\u4e1c\u5317\u8bc1\u5238NET\u4e13\u4e1a\u7248"
        user_name = "\u6d4b\u8bd5"
        qmt = QmtConfig(
            userdata_directory="D:\\" + broker_name + "\\userdata_mini"
        )
        probe = ProbeConfig(
            python_executable=sys.executable,
            xtquant_parent="C:\\Users\\" + user_name + "\\XtQuant",
        )
        supervisor = ProbeSupervisor(probe, qmt, source_root=source_root)

        with patch.dict(
            os.environ,
            {"PYTHONIOENCODING": "cp936", "PYTHONUTF8": "0"},
        ):
            started, reason = supervisor._start_locked()

        self.assertTrue(started, reason)
        process = supervisor._process
        self.assertIsNotNone(process)
        assert process is not None
        assert process.stdin is not None
        assert process.stdout is not None
        request = ProbeRequest(
            operation="shutdown",
            userdata_directory=qmt.userdata_directory,
            xtquant_parent=probe.xtquant_parent,
            session_id=1234,
        )

        try:
            process.stdin.write(request.to_json() + "\n")
            process.stdin.flush()
            response = ProbeResponse.from_json(process.stdout.readline())
            process.wait(timeout=5)
        finally:
            supervisor.stop()

        self.assertEqual(response.request_id, request.request_id)
        self.assertTrue(response.ok)


if __name__ == "__main__":
    unittest.main()
