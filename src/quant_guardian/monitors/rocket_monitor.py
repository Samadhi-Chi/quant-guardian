from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from quant_guardian.config import RocketConfig

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


ERROR_PATTERNS = (
    re.compile(r"NoneType.*total_asset", re.IGNORECASE),
    re.compile(r"query.*account.*fail", re.IGNORECASE),
    re.compile(r"QMT.*连接.*失败"),
)


@dataclass(frozen=True, slots=True)
class RocketObservation:
    active: bool
    error_burst: bool
    reason: str
    log_age_seconds: float | None = None


class RocketMonitor:
    def __init__(self, config: RocketConfig) -> None:
        self.config = config
        self.expected_names = {name.casefold() for name in config.process_names}
        self.log_directory = Path(config.log_directory)

    def _process_active(self) -> bool:
        if not self.config.enabled or psutil is None:
            return False
        for process in psutil.process_iter(["name", "exe", "cmdline"]):
            try:
                name = str(process.info.get("name") or "").casefold()
                if name not in self.expected_names:
                    continue
                combined = " ".join(
                    [
                        str(process.info.get("exe") or ""),
                        *[str(item) for item in (process.info.get("cmdline") or [])],
                    ]
                ).casefold()
                if name == "rocket.exe" or "rocket" in combined:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return False

    def _latest_log(self) -> Path | None:
        if not self.log_directory.exists():
            return None
        files = [path for path in self.log_directory.rglob("*") if path.is_file()]
        return max(files, key=lambda path: path.stat().st_mtime) if files else None

    def observe(self, now: datetime | None = None) -> RocketObservation:
        at = now or datetime.now().astimezone()
        active = self._process_active()
        path = self._latest_log()
        if path is None:
            return RocketObservation(active, False, "Rocket log is unavailable")
        stat = path.stat()
        age = max(0.0, at.timestamp() - stat.st_mtime)
        with path.open("rb") as stream:
            stream.seek(max(0, stat.st_size - 128 * 1024))
            text = stream.read().decode("utf-8", errors="replace")
        matches = sum(len(pattern.findall(text)) for pattern in ERROR_PATTERNS)
        burst = matches >= 5 and age <= 120
        reason = (
            f"Rocket account-query error burst detected ({matches} samples)"
            if burst
            else "Rocket process/log observation is normal"
        )
        return RocketObservation(active, burst, reason, age)
