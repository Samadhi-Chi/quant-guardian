from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import AppConfig
from quant_guardian.monitors.process_monitor import ProcessIdentity
from quant_guardian.recovery import quantclass_controller
from quant_guardian.recovery.quantclass_controller import QuantclassController


class QuantclassControllerTests(unittest.TestCase):
    def make_controller(self, directory: str) -> QuantclassController:
        executable = Path(directory) / "quantclass.exe"
        executable.write_bytes(b"test executable placeholder")
        config = AppConfig()
        config.trade_system.client_executable = str(executable)
        config.recovery.graceful_close_seconds = 0
        return QuantclassController(config)

    def test_launches_configured_client_and_verifies_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            with patch(
                "quant_guardian.recovery.quantclass_controller.subprocess.Popen"
            ) as popen, patch.object(
                controller, "_scan_processes", return_value=((), ())
            ), patch.object(
                controller, "_wait_for_main_process", return_value=4321
            ):
                result = controller.restart(event_id="evt-test")
            self.assertTrue(result.success)
            self.assertEqual(result.details["new_pid"], 4321)
            self.assertEqual(popen.call_args.args[0], [str(controller.executable)])

    def test_refuses_same_name_process_from_unconfigured_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            mismatch = ProcessIdentity(
                pid=99,
                name="quantclass.exe",
                executable=str(Path(directory).parent / "other" / "quantclass.exe"),
                create_time=1,
                responsive=True,
            )
            with patch.object(
                controller, "_scan_processes", return_value=((), (mismatch,))
            ), patch(
                "quant_guardian.recovery.quantclass_controller.subprocess.Popen"
            ) as popen:
                result = controller.restart(event_id="evt-test")
            self.assertFalse(result.success)
            self.assertIn("路径与配置不一致", result.reason)
            popen.assert_not_called()

    def test_never_targets_fuel_zeus_or_rocket_process_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            expected = {
                name.casefold()
                for name in controller.config.trade_system.client_process_names
            }
            self.assertEqual(expected, {"quantclass.exe"})
            self.assertTrue(
                expected.isdisjoint({"fuel.exe", "zeus.exe", "rocket.exe"})
            )

    def test_restart_fails_closed_without_psutil_or_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            with patch.object(quantclass_controller, "psutil", None):
                result = controller.restart(event_id="no-psutil")
            self.assertFalse(result.success)
            self.assertIn("psutil", result.reason)
            controller.executable.unlink()
            result = controller.restart(event_id="missing")
            self.assertFalse(result.success)
            self.assertIn("不存在", result.reason)

    def test_concurrent_restart_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            self.assertTrue(controller._lock.acquire(blocking=False))
            try:
                result = controller.restart(event_id="busy")
            finally:
                controller._lock.release()
            self.assertFalse(result.success)
            self.assertIn("正在执行", result.reason)

    def test_restart_refuses_when_verified_process_cannot_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            item = ProcessIdentity(
                77,
                "quantclass.exe",
                str(controller.executable),
                1.0,
                True,
            )
            with patch.object(
                controller, "_scan_processes", return_value=((item,), ())
            ), patch(
                "quant_guardian.recovery.quantclass_controller.request_graceful_close"
            ), patch(
                "quant_guardian.recovery.quantclass_controller.wait_for_exit",
                side_effect=[(item,), (item,)],
            ), patch(
                "quant_guardian.recovery.quantclass_controller.terminate_exact"
            ) as terminate, patch.object(
                controller, "_launch"
            ) as launch:
                result = controller.restart(event_id="stuck")
            self.assertFalse(result.success)
            self.assertIn("未能安全退出", result.reason)
            terminate.assert_called_once()
            launch.assert_not_called()

    def test_restart_reports_launch_failure_and_verification_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            with patch.object(
                controller, "_scan_processes", return_value=((), ())
            ), patch.object(
                controller, "_launch", return_value=(False, "launch failed")
            ):
                failed = controller.restart(event_id="launch")
            self.assertFalse(failed.success)
            self.assertIn("launch failed", failed.reason)

            with patch.object(
                controller, "_scan_processes", return_value=((), ())
            ), patch.object(
                controller, "_launch", return_value=(True, "started")
            ), patch.object(
                controller, "_wait_for_main_process", return_value=None
            ):
                timeout = controller.restart(event_id="timeout")
            self.assertFalse(timeout.success)
            self.assertTrue(timeout.launched)
            self.assertIn("15秒", timeout.reason)

    def test_launch_rejects_missing_file_and_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            controller.executable.unlink()
            self.assertFalse(controller._launch()[0])
            controller.executable.write_bytes(b"test")
            with patch(
                "quant_guardian.recovery.quantclass_controller.subprocess.Popen",
                side_effect=OSError("denied"),
            ):
                launched, reason = controller._launch()
            self.assertFalse(launched)
            self.assertIn("denied", reason)

    def test_scan_processes_separates_exact_and_same_name_other_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)

            class Item:
                def __init__(self, info):
                    self.info = info

            records = [
                Item(
                    {
                        "pid": 1,
                        "name": "quantclass.exe",
                        "exe": str(controller.executable),
                        "create_time": 10,
                    }
                ),
                Item(
                    {
                        "pid": 2,
                        "name": "quantclass.exe",
                        "exe": str(Path(directory) / "other" / "quantclass.exe"),
                        "create_time": 11,
                    }
                ),
                Item(
                    {
                        "pid": 3,
                        "name": "rocket.exe",
                        "exe": str(Path(directory) / "rocket.exe"),
                        "create_time": 12,
                    }
                ),
            ]
            fake = types.SimpleNamespace(
                process_iter=lambda _fields: records,
                NoSuchProcess=LookupError,
                AccessDenied=PermissionError,
            )
            with patch.object(quantclass_controller, "psutil", fake):
                valid, mismatched = controller._scan_processes()
            self.assertEqual([item.pid for item in valid], [1])
            self.assertEqual([item.pid for item in mismatched], [2])

    def test_wait_for_main_process_returns_oldest_detected_pid_or_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            newer = ProcessIdentity(20, "quantclass.exe", "", 20.0, True)
            older = ProcessIdentity(10, "quantclass.exe", "", 10.0, True)
            with patch.object(
                controller, "_scan_processes", return_value=((newer, older), ())
            ):
                self.assertEqual(controller._wait_for_main_process(timeout=1), 10)
            with patch.object(
                controller, "_scan_processes", return_value=((), ())
            ), patch.object(
                quantclass_controller.time,
                "monotonic",
                side_effect=[0.0, 1.0],
            ):
                self.assertIsNone(controller._wait_for_main_process(timeout=0.5))


if __name__ == "__main__":
    unittest.main()
