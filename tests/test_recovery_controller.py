from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import AppConfig
from quant_guardian.diagnostics.audit import AuditLogger
from quant_guardian.domain.models import (
    HealthSnapshot,
    LogSignal,
    ProbeStatus,
    ProcessStatus,
)
from quant_guardian.monitors.process_monitor import ProcessIdentity, ProcessObservation
from quant_guardian.recovery.controller import RecoveryController
from quant_guardian.safety import SENTINEL_CONTENT, SafetyGate


def snapshot(*, rocket: bool = False) -> HealthSnapshot:
    return HealthSnapshot(
        observed_at=datetime(2026, 8, 13, tzinfo=UTC),
        process_status=ProcessStatus.MISSING,
        probe_status=ProbeStatus.FAILED,
        log_signal=LogSignal.STALE,
        network_available=True,
        rocket_active=rocket,
    )


class FakeProcessMonitor:
    def __init__(
        self,
        status: ProcessStatus = ProcessStatus.MISSING,
        identities: tuple[ProcessIdentity, ...] = (),
    ) -> None:
        self.status = status
        self.identities = identities

    def observe(self):
        return ProcessObservation(self.status, self.identities, "test")

    def validated_processes(self):
        return self.identities


class ExplodingProcessMonitor:
    def observe(self):
        raise AssertionError("process monitor must not run when recovery is blocked")

    def validated_processes(self):
        raise AssertionError("process identities must not be read when recovery is blocked")


class RecoveryControllerTests(unittest.TestCase):
    def make_controller(
        self,
        directory: str,
        *,
        monitor=None,
        recover: bool = True,
    ) -> RecoveryController:
        root = Path(directory)
        launcher = root / "qmt" / "bin.x64" / "XtItClient.exe"
        working = root / "qmt" / "config"
        launcher.parent.mkdir(parents=True)
        working.mkdir(parents=True)
        launcher.write_bytes(b"launcher")
        config = AppConfig()
        config.qmt.launcher = str(launcher)
        config.qmt.working_directory = str(working)
        config.recovery.graceful_close_seconds = 0
        config.mode = "recover" if recover else "observe"
        sentinel = root / "RECOVERY_ENABLED"
        if recover:
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
        return RecoveryController(
            config,
            monitor or FakeProcessMonitor(),
            SafetyGate(config, sentinel),
            AuditLogger(root / "logs"),
        )

    def test_automatic_recovery_refuses_rocket_active_before_any_process_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(
                directory, monitor=ExplodingProcessMonitor()
            )
            controller.config.recovery.allow_qmt_restart_while_rocket_active = False
            result = controller.recover(snapshot(rocket=True), event_id="test-event")
            self.assertFalse(result.success)
            self.assertFalse(result.launched)
            self.assertFalse(result.live_action)
            self.assertIn("Rocket is active", result.reason)

    def test_automatic_gate_blocks_but_manual_restart_bypasses_only_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory, recover=False)
            blocked = controller.recover(snapshot(), event_id="automatic")
            self.assertFalse(blocked.live_action)
            self.assertIn("观察模式", blocked.reason)
            with patch(
                "quant_guardian.recovery.controller.subprocess.Popen"
            ) as popen:
                manual = controller.restart_manually(
                    snapshot(), event_id="manual"
                )
            self.assertTrue(manual.success)
            self.assertTrue(manual.live_action)
            popen.assert_called_once()

    def test_identity_mismatch_refuses_termination_and_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = FakeProcessMonitor(ProcessStatus.IDENTITY_MISMATCH)
            controller = self.make_controller(directory, monitor=monitor)
            with patch.object(controller, "_launch") as launch:
                result = controller.recover(snapshot(), event_id="mismatch")
            self.assertFalse(result.success)
            self.assertTrue(result.live_action)
            self.assertIn("identity mismatch", result.reason)
            launch.assert_not_called()

    def test_concurrent_recovery_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            self.assertTrue(controller._lock.acquire(blocking=False))
            try:
                result = controller.recover(snapshot(), event_id="busy")
            finally:
                controller._lock.release()
            self.assertFalse(result.success)
            self.assertTrue(result.live_action)
            self.assertIn("already running", result.reason)

    def test_validated_processes_close_terminate_then_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = ProcessIdentity(
                42,
                "XtMiniQmt.exe",
                str(Path(directory) / "qmt" / "bin.x64" / "XtMiniQmt.exe"),
                1.0,
                True,
            )
            controller = self.make_controller(
                directory, monitor=FakeProcessMonitor(identities=(item,))
            )
            with patch(
                "quant_guardian.recovery.controller.request_graceful_close"
            ) as graceful, patch(
                "quant_guardian.recovery.controller.wait_for_exit",
                return_value=(item,),
            ), patch(
                "quant_guardian.recovery.controller.terminate_exact"
            ) as terminate, patch.object(
                controller, "_launch", return_value=(True, "started")
            ):
                result = controller.recover(snapshot(), event_id="recover")
            self.assertTrue(result.success)
            graceful.assert_called_once_with({42})
            terminate.assert_called_once_with(
                (item,), Path(controller.config.qmt.launcher).parent
            )
            self.assertEqual(result.details["validated_process_count"], 1)

    def test_launch_validates_files_and_reports_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            launcher = Path(controller.config.qmt.launcher)
            launcher.unlink()
            self.assertIn("does not exist", controller._launch()[1])
            launcher.write_bytes(b"launcher")
            working = Path(controller.config.qmt.working_directory)
            working.rmdir()
            self.assertIn("does not exist", controller._launch()[1])
            working.mkdir()
            with patch(
                "quant_guardian.recovery.controller.subprocess.Popen",
                side_effect=OSError("blocked"),
            ):
                launched, reason = controller._launch()
            self.assertFalse(launched)
            self.assertIn("blocked", reason)

    def test_launch_uses_exact_configured_executable_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            with patch(
                "quant_guardian.recovery.controller.subprocess.Popen"
            ) as popen:
                launched, _reason = controller._launch()
            self.assertTrue(launched)
            self.assertEqual(
                popen.call_args.args[0], [controller.config.qmt.launcher]
            )
            self.assertEqual(
                popen.call_args.kwargs["cwd"],
                controller.config.qmt.working_directory,
            )


if __name__ == "__main__":
    unittest.main()
