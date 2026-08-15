from __future__ import annotations

import types
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.monitors.process_monitor import ProcessIdentity
from quant_guardian.recovery import windows_process_control as process_control


def identity(
    pid: int = 42,
    *,
    created: float = 10.0,
    executable: str = r"C:\QMT\bin.x64\XtMiniQmt.exe",
) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        name="XtMiniQmt.exe",
        executable=executable,
        create_time=created,
        responsive=True,
    )


class FakeProcess:
    def __init__(self, item: ProcessIdentity) -> None:
        self.pid = item.pid
        self._created = item.create_time
        self._executable = item.executable
        self.terminated = False
        self.killed = False

    def create_time(self) -> float:
        return self._created

    def exe(self) -> str:
        return self._executable

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class FakePsutil:
    NoSuchProcess = LookupError
    AccessDenied = PermissionError

    def __init__(self, processes: dict[int, FakeProcess]) -> None:
        self.processes = processes
        self.alive: list[FakeProcess] = []
        self.existing: set[int] = set(processes)

    def Process(self, pid: int) -> FakeProcess:
        if pid not in self.processes:
            raise self.NoSuchProcess(pid)
        return self.processes[pid]

    def wait_procs(self, processes, timeout):
        _ = timeout
        return [], list(self.alive)

    def pid_exists(self, pid: int) -> bool:
        return pid in self.existing


class WindowsProcessControlTests(unittest.TestCase):
    def test_identity_match_requires_same_creation_time_and_qmt_path(self) -> None:
        item = identity()
        fake = FakePsutil({item.pid: FakeProcess(item)})
        with patch.object(process_control, "psutil", fake):
            self.assertTrue(
                process_control._identity_still_matches(
                    item, Path(r"C:\QMT\bin.x64")
                )
            )
            reused = identity(created=9.0)
            self.assertFalse(
                process_control._identity_still_matches(
                    reused, Path(r"C:\QMT\bin.x64")
                )
            )
            outside = identity(executable=r"C:\Malware\XtMiniQmt.exe")
            fake.processes[item.pid] = FakeProcess(outside)
            self.assertFalse(
                process_control._identity_still_matches(
                    item, Path(r"C:\QMT\bin.x64")
                )
            )

    def test_identity_check_fails_closed_without_psutil_or_process(self) -> None:
        item = identity()
        with patch.object(process_control, "psutil", None):
            self.assertFalse(
                process_control._identity_still_matches(
                    item, Path(r"C:\QMT\bin.x64")
                )
            )
        with patch.object(process_control, "psutil", FakePsutil({})):
            self.assertFalse(
                process_control._identity_still_matches(
                    item, Path(r"C:\QMT\bin.x64")
                )
            )

    def test_terminate_exact_kills_only_still_matching_identity(self) -> None:
        valid = identity(42)
        reused = identity(43, created=10.0)
        valid_process = FakeProcess(valid)
        reused_process = FakeProcess(
            identity(43, created=11.0, executable=valid.executable)
        )
        fake = FakePsutil({42: valid_process, 43: reused_process})
        fake.alive = [valid_process]
        with patch.object(process_control, "psutil", fake):
            terminated = process_control.terminate_exact(
                (valid, reused), Path(r"C:\QMT\bin.x64"), timeout=0
            )
        self.assertEqual(terminated, (42,))
        self.assertTrue(valid_process.terminated)
        self.assertTrue(valid_process.killed)
        self.assertFalse(reused_process.terminated)
        self.assertFalse(reused_process.killed)

    def test_terminate_exact_requires_psutil(self) -> None:
        with patch.object(process_control, "psutil", None):
            with self.assertRaisesRegex(RuntimeError, "psutil"):
                process_control.terminate_exact(
                    (identity(),), Path(r"C:\QMT\bin.x64")
                )

    def test_wait_for_exit_returns_only_remaining_processes(self) -> None:
        item = identity()
        fake = FakePsutil({item.pid: FakeProcess(item)})
        fake.existing.clear()
        with patch.object(process_control, "psutil", fake), patch.object(
            process_control.time, "monotonic", side_effect=[0.0, 0.0]
        ):
            self.assertEqual(process_control.wait_for_exit((item,), 1.0), ())
        with patch.object(process_control, "psutil", fake):
            self.assertEqual(process_control.wait_for_exit((item,), 0.0), (item,))

    def test_graceful_close_posts_only_to_requested_visible_process(self) -> None:
        class User32:
            def GetWindowThreadProcessId(self, _hwnd, pointer):
                pointer._obj.value = 42

            def IsWindowVisible(self, _hwnd):
                return True

            def PostMessageW(self, _hwnd, message, _wparam, _lparam):
                return message == process_control.WM_CLOSE

            def EnumWindows(self, callback, _parameter):
                callback(100, 0)

        fake_windll = types.SimpleNamespace(user32=User32())
        with patch.object(process_control.ctypes, "windll", fake_windll):
            self.assertEqual(process_control.request_graceful_close({42}), 1)
        self.assertEqual(process_control.request_graceful_close(set()), 0)


if __name__ == "__main__":
    unittest.main()
