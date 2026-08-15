from __future__ import annotations

import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from quant_guardian.config import ProbeConfig, QmtConfig
from quant_guardian.domain.models import ProbeStatus
from quant_guardian.probe.protocol import ProbeRequest, ProbeResponse


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    status: ProbeStatus
    reason: str
    latency_ms: int = 0
    account_status: str = "unknown"
    account_ref: str = ""
    details: dict[str, object] | None = None


class ProbeSupervisor:
    _COLD_START_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        probe_config: ProbeConfig,
        qmt_config: QmtConfig,
        *,
        source_root: Path | None = None,
    ) -> None:
        self.probe_config = probe_config
        self.qmt_config = qmt_config
        if source_root is not None:
            self.source_root = source_root
        elif getattr(sys, "frozen", False):
            bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            self.source_root = bundle_root / "probe_runtime"
        else:
            self.source_root = Path(__file__).resolve().parents[2]
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._last_start = 0.0
        self._used_session_ids = {probe_config.session_id}
        self._consecutive_timeouts = 0

    def _rotate_session_locked(self) -> None:
        session_id = self.probe_config.session_id
        while session_id in self._used_session_ids:
            session_id = 100_000_000 + secrets.randbelow(1_900_000_000)
        self._used_session_ids.add(session_id)
        self.probe_config = replace(self.probe_config, session_id=session_id)
        self._last_start = 0.0

    def reset_after_recovery(self) -> None:
        """Discard a worker/session that may refer to the pre-restart QMT."""

        with self._lock:
            self._stop_locked()
            self._rotate_session_locked()
            self._consecutive_timeouts = 0

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _start_locked(self) -> tuple[bool, str]:
        if self._process is not None and self._process.poll() is None:
            return True, "already running"
        executable = Path(self.probe_config.python_executable)
        if not self.probe_config.python_executable:
            return False, "Python 3.11 probe executable is not configured"
        if not executable.exists():
            return False, f"probe executable does not exist: {executable}"
        elapsed = time.monotonic() - self._last_start
        if elapsed < 3.0:
            return False, f"probe restart cooldown active for {3.0 - elapsed:.1f} seconds"
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(self.source_root) + (os.pathsep + existing if existing else "")
        # The parent pipe is explicitly UTF-8. Force the isolated worker to use
        # the same encoding even on Chinese Windows installations where the
        # redirected stdio default can otherwise be CP936. A CP936 decoder may
        # consume a JSON path backslash as the trail byte of a multibyte
        # character, turning a valid JSON path into an invalid escape.
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"

        self._process = subprocess.Popen(
            [str(executable), "-s", "-m", "quant_guardian.probe.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        self._last_start = time.monotonic()
        return True, "started"

    def _response_timeout(self, *, cold_start: bool) -> float:
        if cold_start:
            return max(
                self.probe_config.timeout_seconds,
                self._COLD_START_TIMEOUT_SECONDS,
            )
        return self.probe_config.timeout_seconds

    def _request(
        self,
        operation: str,
        *,
        market: str = "SH",
        start_date: str = "",
        end_date: str = "",
    ) -> ProbeObservation:
        if not self.probe_config.enabled:
            return ProbeObservation(ProbeStatus.UNAVAILABLE, "read-only probe is disabled")
        with self._lock:
            started, reason = self._start_locked()
            if not started:
                return ProbeObservation(ProbeStatus.UNAVAILABLE, reason)
            cold_start = reason == "started"
            assert self._process is not None
            process = self._process
            assert process.stdin is not None and process.stdout is not None
            request = ProbeRequest(
                operation=operation,  # type: ignore[arg-type]
                userdata_directory=self.qmt_config.userdata_directory,
                xtquant_parent=self.probe_config.xtquant_parent,
                session_id=self.probe_config.session_id,
                account_id_protected=self.probe_config.account_id_protected,
                market=market,
                start_date=start_date,
                end_date=end_date,
            )
            try:
                process.stdin.write(request.to_json() + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._stop_locked()
                return ProbeObservation(ProbeStatus.FAILED, f"probe pipe write failed: {exc}")

            result_queue: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

            def read_response() -> None:
                try:
                    result_queue.put(process.stdout.readline())
                except BaseException as exc:  # propagate reader failure to caller
                    result_queue.put(exc)

            reader = threading.Thread(target=read_response, daemon=True)
            reader.start()
            try:
                timeout = self._response_timeout(cold_start=cold_start)
                result = result_queue.get(timeout=timeout)
            except queue.Empty:
                self._stop_locked()
                self._consecutive_timeouts += 1
                if self._consecutive_timeouts >= 2:
                    # XtQuant can retain a dead client identity even after the
                    # worker process is replaced.  Give the third health sample
                    # a genuinely fresh session before the state machine is
                    # allowed to restart QMT.
                    self._rotate_session_locked()
                    self._consecutive_timeouts = 0
                return ProbeObservation(
                    ProbeStatus.TIMEOUT,
                    f"read-only probe exceeded {timeout:g} seconds",
                )
            if isinstance(result, BaseException):
                self._stop_locked()
                return ProbeObservation(ProbeStatus.FAILED, f"probe reader failed: {result}")
            if not result:
                stderr = ""
                if process.stderr is not None and process.poll() is not None:
                    stderr = process.stderr.read(2048)
                self._stop_locked()
                return ProbeObservation(
                    ProbeStatus.FAILED,
                    f"probe exited without a response: {stderr.strip()}",
                )
            try:
                response = ProbeResponse.from_json(result)
            except (ValueError, TypeError) as exc:
                self._stop_locked()
                return ProbeObservation(ProbeStatus.FAILED, f"invalid probe response: {exc}")
            if response.request_id != request.request_id:
                self._stop_locked()
                if response.fatal and response.request_id == "unknown":
                    return ProbeObservation(
                        ProbeStatus.FAILED,
                        "probe worker rejected request before correlation: "
                        f"{response.reason}",
                    )
                return ProbeObservation(ProbeStatus.FAILED, "probe response ID mismatch")
            if response.fatal:
                self._stop_locked()
            status = ProbeStatus.HEALTHY if response.ok else ProbeStatus.FAILED
            if status is ProbeStatus.HEALTHY:
                self._consecutive_timeouts = 0
            return ProbeObservation(
                status,
                response.reason,
                response.latency_ms,
                response.account_status,
                response.account_ref,
                response.details,
            )

    def health(self) -> ProbeObservation:
        return self._request("health")

    def reconcile(self) -> ProbeObservation:
        return self._request("reconcile")

    def calendar(
        self,
        *,
        market: str = "SH",
        start_date: str = "",
        end_date: str = "",
    ) -> ProbeObservation:
        return self._request(
            "calendar",
            market=market,
            start_date=start_date,
            end_date=end_date,
        )
