from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from quant_guardian.domain.models import ProbeStatus
from quant_guardian.probe.protocol import ProbeRequest, ProbeResponse
from quant_guardian.probe.supervisor import ProbeObservation, ProbeSupervisor, response_status


class BrokerProbeSupervisor(ProbeSupervisor):
    """Isolate the broker's CPython/XTQuant ABI, including Python 3.6 pythonw.

    The broker installation is never modified. Private, correlated file IPC is
    used because pythonw does not reliably provide standard streams.
    """

    _COLD_START_TIMEOUT_SECONDS = 8.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ipc: tempfile.TemporaryDirectory | None = None

    def _stop_locked(self) -> None:
        super()._stop_locked()
        if self._ipc is not None:
            self._ipc.cleanup()
            self._ipc = None

    def _start_locked(self) -> tuple[bool, str]:
        if self._process is not None and self._process.poll() is None:
            return True, "already running"
        executable = Path(self.probe_config.python_executable)
        expected = Path(self.qmt_config.launcher).parent
        library = expected / "Lib" / "site-packages"
        if (
            executable.resolve() != (expected / "pythonw.exe").resolve()
            or Path(self.probe_config.xtquant_parent).resolve() != library.resolve()
        ):
            return False, "Plan A 必须使用当前券商安装内配套的 Python 和 XTQuant"
        if not executable.is_file() or not (library / "xtquant").is_dir():
            return False, "券商配套 Python/XTQuant 不完整，需人工完成安装"
        self._stop_locked()
        worker = self.source_root / "quant_guardian" / "probe" / "broker_worker.py"
        if not worker.is_file():
            return False, "Plan A 只读辅助程序未包含在安装包中"
        self._ipc = tempfile.TemporaryDirectory(prefix="quant-guardian-broker-")
        environment = os.environ.copy()
        for key in ("PYTHONPATH", "PYTHONHOME"):
            environment.pop(key, None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PATH"] = os.pathsep.join(
            [str(expected), str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")]
        )
        try:
            self._process = subprocess.Popen(
                [str(executable), "-B", "-s", str(worker), "--directory", self._ipc.name],
                cwd=str(expected), env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            self._stop_locked()
            return False, "无法启动券商只读辅助程序，请检查权限与配套运行环境"
        self._last_start = time.monotonic()
        return True, "started"

    def _request(self, operation: str, **kwargs) -> ProbeObservation:
        if not self.probe_config.enabled:
            return ProbeObservation(ProbeStatus.UNAVAILABLE, "read-only probe is disabled")
        with self._lock:
            started, reason = self._start_locked()
            if not started:
                return ProbeObservation(ProbeStatus.UNAVAILABLE, reason)
            assert self._ipc is not None
            request = ProbeRequest(
                operation=operation,  # type: ignore[arg-type]
                userdata_directory=self.qmt_config.userdata_directory,
                xtquant_parent=self.probe_config.xtquant_parent,
                session_id=self.probe_config.session_id,
                account_id_protected=self.probe_config.account_id_protected,
                **kwargs,
            )
            directory = Path(self._ipc.name)
            response_path = directory / (request.request_id + ".json")
            document = json.loads(request.to_json())
            document["query_timeout_seconds"] = max(0.5, self.probe_config.timeout_seconds * 0.7)
            temporary = directory / "request.tmp"
            temporary.write_text(json.dumps(document), encoding="utf-8")
            temporary.replace(directory / "request.json")
            deadline = time.monotonic() + self._response_timeout(cold_start=reason == "started")
            while time.monotonic() < deadline:
                if response_path.is_file():
                    try:
                        response = ProbeResponse.from_json(response_path.read_text(encoding="utf-8"))
                        if response.request_id != request.request_id:
                            raise ValueError("correlation mismatch")
                    except (OSError, ValueError, TypeError):
                        self._stop_locked()
                        return ProbeObservation(ProbeStatus.UNAVAILABLE, "券商只读响应无效，需核对探针环境")
                    response_path.unlink(missing_ok=True)
                    if response.fatal:
                        self._stop_locked()
                    return ProbeObservation(
                        response_status(response), response.reason, response.latency_ms,
                        response.account_status, response.account_ref, response.details,
                    )
                if self._process is None or self._process.poll() is not None:
                    self._stop_locked()
                    return ProbeObservation(
                        ProbeStatus.UNAVAILABLE,
                        "券商只读辅助程序退出；检查运行环境或文件校验，不能据此重启QMT",
                    )
                time.sleep(0.02)
            self._stop_locked()
            self._rotate_session_locked()
            return ProbeObservation(ProbeStatus.TIMEOUT, "券商独立只读查询超时")
