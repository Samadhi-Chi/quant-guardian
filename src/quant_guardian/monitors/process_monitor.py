from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant_guardian.config import QmtConfig
from quant_guardian.domain.models import ProcessStatus

try:
    import psutil
except ImportError:  # pragma: no cover - exercised on dependency-free startup
    psutil = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    name: str
    executable: str
    create_time: float
    responsive: bool


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    status: ProcessStatus
    processes: tuple[ProcessIdentity, ...]
    reason: str


def _same_or_child_path(candidate: str, directory: Path) -> bool:
    try:
        normalized = Path(candidate).resolve(strict=False)
        expected = directory.resolve(strict=False)
        return normalized == expected or expected in normalized.parents
    except (OSError, RuntimeError):
        return False


def _window_responsive(pid: int) -> bool:
    if os.name != "nt":
        return True
    user32 = ctypes.windll.user32
    windows: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True

    user32.EnumWindows(callback_type(callback), 0)
    if not windows:
        return True
    return not all(bool(user32.IsHungAppWindow(hwnd)) for hwnd in windows)


class QmtProcessMonitor:
    def __init__(self, config: QmtConfig) -> None:
        self.config = config
        self.expected_directory = Path(config.launcher).parent
        self.expected_names = {name.casefold() for name in config.process_names}
        self.main_name = "xtminiqmt.exe"

    def observe(self) -> ProcessObservation:
        if psutil is None:
            return ProcessObservation(
                ProcessStatus.UNKNOWN,
                (),
                "psutil is not installed",
            )
        valid: list[ProcessIdentity] = []
        mismatched: list[ProcessIdentity] = []
        for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
            try:
                info: dict[str, Any] = process.info
                name = str(info.get("name") or "")
                if name.casefold() not in self.expected_names:
                    continue
                executable = str(info.get("exe") or "")
                identity = ProcessIdentity(
                    pid=int(info["pid"]),
                    name=name,
                    executable=executable,
                    create_time=float(info.get("create_time") or 0),
                    responsive=_window_responsive(int(info["pid"])),
                )
                if _same_or_child_path(executable, self.expected_directory):
                    valid.append(identity)
                else:
                    mismatched.append(identity)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue

        if mismatched and not valid:
            return ProcessObservation(
                ProcessStatus.IDENTITY_MISMATCH,
                tuple(mismatched),
                "matching process name was found outside the configured QMT directory",
            )
        main = next((item for item in valid if item.name.casefold() == self.main_name), None)
        if main is None:
            return ProcessObservation(
                ProcessStatus.MISSING,
                tuple(valid),
                "XtMiniQmt.exe was not found in the configured directory",
            )
        if not main.responsive:
            return ProcessObservation(
                ProcessStatus.UNRESPONSIVE,
                tuple(valid),
                "QMT main window is unresponsive",
            )
        return ProcessObservation(
            ProcessStatus.HEALTHY,
            tuple(valid),
            "QMT process identity and responsiveness are valid",
        )

    def validated_processes(self) -> tuple[ProcessIdentity, ...]:
        observation = self.observe()
        if observation.status is ProcessStatus.IDENTITY_MISMATCH:
            return ()
        return observation.processes
