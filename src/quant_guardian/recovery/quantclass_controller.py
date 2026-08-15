from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from quant_guardian.config import AppConfig
from quant_guardian.domain.models import RecoveryResult
from quant_guardian.monitors.process_monitor import ProcessIdentity
from quant_guardian.recovery.windows_process_control import (
    request_graceful_close,
    terminate_exact,
    wait_for_exit,
)

try:
    import psutil
except ImportError:  # pragma: no cover - production bundle includes psutil
    psutil = None  # type: ignore[assignment]


def _path_key(value: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(value).resolve(strict=False)))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(str(value)))


class QuantclassController:
    """Operator-only controller for the Quantclass desktop client.

    It deliberately targets only the configured Electron executable. Fuel,
    Aqua, Zeus and Rocket processes are not selected or terminated here.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.Lock()

    @property
    def executable(self) -> Path:
        return Path(self.config.trade_system.client_executable)

    def _scan_processes(
        self,
    ) -> tuple[tuple[ProcessIdentity, ...], tuple[ProcessIdentity, ...]]:
        if psutil is None:
            return (), ()
        expected_executable = _path_key(self.executable)
        expected_names = {
            name.casefold() for name in self.config.trade_system.client_process_names
        }
        valid: list[ProcessIdentity] = []
        mismatched: list[ProcessIdentity] = []
        for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
            try:
                info: dict[str, Any] = process.info
                name = str(info.get("name") or "")
                if name.casefold() not in expected_names:
                    continue
                identity = ProcessIdentity(
                    pid=int(info["pid"]),
                    name=name,
                    executable=str(info.get("exe") or ""),
                    create_time=float(info.get("create_time") or 0),
                    responsive=True,
                )
                if _path_key(identity.executable) == expected_executable:
                    valid.append(identity)
                else:
                    mismatched.append(identity)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, RuntimeError):
                continue
        return tuple(valid), tuple(mismatched)

    def _launch(self) -> tuple[bool, str]:
        executable = self.executable
        if not executable.is_file():
            return False, f"Quantclass客户端不存在：{executable}"
        working_directory = executable.parent
        try:
            subprocess.Popen(
                [str(executable)],
                cwd=str(working_directory),
                close_fds=True,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except OSError as exc:
            return False, f"启动Quantclass客户端失败：{exc}"
        return True, "Quantclass客户端启动命令已执行"

    def _wait_for_main_process(self, timeout: float = 15.0) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            valid, _mismatched = self._scan_processes()
            if valid:
                return min(valid, key=lambda item: item.create_time).pid
            time.sleep(0.25)
        return None

    def restart(self, *, event_id: str) -> RecoveryResult:
        if psutil is None:
            return RecoveryResult(
                False,
                False,
                False,
                "psutil不可用，无法安全识别Quantclass进程",
            )
        if not self._lock.acquire(blocking=False):
            return RecoveryResult(
                False,
                False,
                True,
                "另一个Quantclass重启操作正在执行",
            )
        try:
            executable = self.executable
            if not executable.is_file():
                return RecoveryResult(
                    False,
                    False,
                    True,
                    f"Quantclass客户端不存在：{executable}",
                )
            valid, mismatched = self._scan_processes()
            if mismatched and not valid:
                return RecoveryResult(
                    False,
                    False,
                    True,
                    "发现同名进程但其路径与配置不一致；为避免误杀已拒绝重启",
                    details={
                        "event_id": event_id,
                        "mismatched_pids": [item.pid for item in mismatched],
                    },
                )

            previous_pids = [identity.pid for identity in valid]
            if valid:
                request_graceful_close(set(previous_pids))
                remaining = wait_for_exit(
                    valid,
                    timeout=float(self.config.recovery.graceful_close_seconds),
                )
                if remaining:
                    terminate_exact(remaining, executable.parent)
                    remaining = wait_for_exit(remaining, timeout=5.0)
                if remaining:
                    return RecoveryResult(
                        False,
                        False,
                        True,
                        "Quantclass残留进程未能安全退出",
                        details={
                            "event_id": event_id,
                            "remaining_pids": [item.pid for item in remaining],
                        },
                    )

            launched, reason = self._launch()
            if not launched:
                return RecoveryResult(
                    False,
                    False,
                    True,
                    reason,
                    details={"event_id": event_id, "previous_pids": previous_pids},
                )
            new_pid = self._wait_for_main_process()
            if new_pid is None:
                return RecoveryResult(
                    False,
                    True,
                    True,
                    "Quantclass启动命令已执行，但15秒内未检测到客户端进程",
                    details={"event_id": event_id, "previous_pids": previous_pids},
                )
            return RecoveryResult(
                True,
                True,
                True,
                "Quantclass客户端已重新启动",
                details={
                    "event_id": event_id,
                    "previous_pids": previous_pids,
                    "new_pid": new_pid,
                    "mismatched_process_count": len(mismatched),
                },
            )
        finally:
            self._lock.release()
