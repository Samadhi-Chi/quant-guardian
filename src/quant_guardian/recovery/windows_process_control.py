from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

from quant_guardian.monitors.process_monitor import ProcessIdentity

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


WM_CLOSE = 0x0010


def request_graceful_close(pids: set[int]) -> int:
    if os.name != "nt" or not pids:
        return 0
    user32 = ctypes.windll.user32
    posted = 0
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _: int) -> bool:
        nonlocal posted
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value in pids and user32.IsWindowVisible(hwnd):
            if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                posted += 1
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return posted


def _identity_still_matches(identity: ProcessIdentity, expected_directory: Path) -> bool:
    if psutil is None:
        return False
    try:
        process = psutil.Process(identity.pid)
        if abs(process.create_time() - identity.create_time) > 0.01:
            return False
        executable = Path(process.exe()).resolve(strict=False)
        directory = expected_directory.resolve(strict=False)
        return executable == directory or directory in executable.parents
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, RuntimeError):
        return False


def wait_for_exit(
    identities: tuple[ProcessIdentity, ...], timeout: float
) -> tuple[ProcessIdentity, ...]:
    deadline = time.monotonic() + timeout
    remaining = list(identities)
    while remaining and time.monotonic() < deadline:
        if psutil is None:
            break
        remaining = [
            identity for identity in remaining if psutil.pid_exists(identity.pid)
        ]
        if remaining:
            time.sleep(0.25)
    return tuple(remaining)


def terminate_exact(
    identities: tuple[ProcessIdentity, ...],
    expected_directory: Path,
    timeout: float = 5.0,
) -> tuple[int, ...]:
    if psutil is None:
        raise RuntimeError("psutil is required for exact process termination")
    terminated: list[int] = []
    processes: list[object] = []
    for identity in identities:
        if not _identity_still_matches(identity, expected_directory):
            continue
        try:
            process = psutil.Process(identity.pid)
            process.terminate()
            terminated.append(identity.pid)
            processes.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for process in alive:
        try:
            identity = next(item for item in identities if item.pid == process.pid)
        except StopIteration:
            continue
        if not _identity_still_matches(identity, expected_directory):
            continue
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return tuple(terminated)