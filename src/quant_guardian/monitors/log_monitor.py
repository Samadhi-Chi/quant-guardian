from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from quant_guardian.domain.models import LogSignal

POSITIVE_PATTERNS = (
    re.compile(r"account.*login success", re.IGNORECASE),
    re.compile(r"push accountdetail", re.IGNORECASE),
    re.compile(r"query.*success", re.IGNORECASE),
)
DISCONNECT_PATTERNS = (
    re.compile(r"proxy disconnect", re.IGNORECASE),
    re.compile(r"End of file", re.IGNORECASE),
)
LOGIN_FAILURE_PATTERNS = (
    re.compile(r"account login failed", re.IGNORECASE),
    re.compile(r"未建立连接"),
)
LOGIN_MANUAL_PATTERNS = (
    re.compile(r"验证码"),
    re.compile(r"captcha", re.IGNORECASE),
    re.compile(r"二次认证"),
    re.compile(r"请输入密码"),
)


@dataclass(frozen=True, slots=True)
class LogObservation:
    signal: LogSignal
    reason: str
    path: str = ""
    last_modified: datetime | None = None
    login_requires_manual: bool = False


def classify_lines(lines: list[str]) -> tuple[LogSignal, str, bool]:
    latest: tuple[int, LogSignal, str] | None = None
    manual = False
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in LOGIN_MANUAL_PATTERNS):
            manual = True
        if any(pattern.search(line) for pattern in DISCONNECT_PATTERNS):
            latest = (index, LogSignal.EXPLICIT_DISCONNECT, "explicit broker proxy disconnect")
        elif any(pattern.search(line) for pattern in LOGIN_FAILURE_PATTERNS):
            latest = (index, LogSignal.LOGIN_FAILURE, "account login failure")
        elif any(pattern.search(line) for pattern in POSITIVE_PATTERNS):
            latest = (index, LogSignal.POSITIVE, "QMT login or account push succeeded")
    if latest:
        return latest[1], latest[2], manual
    return LogSignal.NEUTRAL, "no decisive QMT log signal", manual


class QmtLogMonitor:
    _MAX_TRACKED_FILES = 16

    def __init__(self, log_directory: Path, stale_seconds: int = 30) -> None:
        self.log_directory = log_directory
        self.stale_seconds = stale_seconds
        self._offsets: dict[Path, int] = {}
        self._initialized = False
        self._last_signal = LogSignal.NEUTRAL
        self._last_reason = "no log sample yet"
        self._manual = False

    def _recent_logs(self) -> list[Path]:
        if not self.log_directory.exists():
            return []
        candidates: list[tuple[float, Path]] = []
        for path in self.log_directory.rglob("*"):
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in candidates[: self._MAX_TRACKED_FILES]]

    @staticmethod
    def _decode(data: bytes) -> str:
        decoded = data.decode("utf-8", errors="replace")
        if decoded.count("\ufffd") > max(3, len(decoded) // 100):
            decoded = data.decode("gb18030", errors="replace")
        return decoded

    def _read_new_lines(self, path: Path, size: int) -> list[str]:
        current = self._offsets.get(path)
        if current is None:
            if not self._initialized:
                self._offsets[path] = size
                return []
            current = max(0, size - 128 * 1024)
        if size < current:
            current = 0
        if size <= current:
            self._offsets[path] = size
            return []
        with path.open("rb") as stream:
            stream.seek(current)
            data = stream.read(256 * 1024)
            self._offsets[path] = stream.tell()
        return self._decode(data).splitlines()

    def observe(self, now: datetime | None = None) -> LogObservation:
        at = now or datetime.now().astimezone()
        paths = self._recent_logs()
        if not paths:
            return LogObservation(LogSignal.UNAVAILABLE, "QMT log directory is unavailable")

        decisive: tuple[float, LogSignal, str] | None = None
        manual = self._manual
        stats: dict[Path, object] = {}
        readable_stats: dict[Path, object] = {}
        read_errors: list[tuple[Path, OSError]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            stats[path] = stat
            try:
                lines = self._read_new_lines(path, stat.st_size)
            except OSError as exc:
                # QMT can rotate or briefly lock a log while it is being
                # written.  Runtime logs are supporting evidence, so a locked
                # file must not abort the process/API/account health cycle.
                # Keep the last valid evidence internally and retry next time.
                read_errors.append((path, exc))
                continue
            readable_stats[path] = stat
            if not lines:
                continue
            signal, reason, observed_manual = classify_lines(lines)
            manual = manual or observed_manual
            if signal is not LogSignal.NEUTRAL:
                candidate = (stat.st_mtime, signal, reason)
                if decisive is None or candidate[0] >= decisive[0]:
                    decisive = candidate

        self._initialized = True
        self._manual = manual
        if decisive is not None:
            self._last_signal = decisive[1]
            self._last_reason = decisive[2]

        latest_path = next((path for path in paths if path in readable_stats), None)
        if latest_path is None:
            if read_errors:
                failed_path, error = read_errors[0]
                failed_stat = stats.get(failed_path)
                modified = (
                    datetime.fromtimestamp(failed_stat.st_mtime, tz=at.tzinfo)
                    if failed_stat is not None
                    else None
                )
                return LogObservation(
                    LogSignal.UNAVAILABLE,
                    "QMT log is temporarily unreadable; previous evidence was "
                    f"retained ({type(error).__name__})",
                    str(failed_path),
                    modified,
                    self._manual,
                )
            return LogObservation(
                LogSignal.UNAVAILABLE, "QMT log directory changed during observation"
            )
        latest_stat = readable_stats[latest_path]
        modified = datetime.fromtimestamp(latest_stat.st_mtime, tz=at.tzinfo)
        age = (at - modified).total_seconds()
        if age > self.stale_seconds:
            return LogObservation(
                LogSignal.STALE,
                f"QMT log has not changed for {int(age)} seconds",
                str(latest_path),
                modified,
                self._manual,
            )
        return LogObservation(
            self._last_signal,
            self._last_reason,
            str(latest_path),
            modified,
            self._manual,
        )
