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
_BUSINESS_HEARTBEAT = re.compile(
    r"INFO:root:(\d{2}:\d{2}:\d{2}).*?"
    r"ex_api\.(?:refresh_entrusts|simple_statistics).*?(?:运行成功|success)",
    re.IGNORECASE,
)
_LOG_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True, slots=True)
class RocketObservation:
    active: bool
    error_burst: bool
    reason: str
    log_age_seconds: float | None = None
    business_healthy: bool = False
    business_age_seconds: float | None = None
    heartbeat_source: str = "none"
    business_health_known: bool = False


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

    @staticmethod
    def _business_heartbeat_at(path: Path, text: str) -> datetime | None:
        matches = list(_BUSINESS_HEARTBEAT.finditer(text))
        date_match = _LOG_DATE.search(path.name)
        if not matches or date_match is None:
            return None
        try:
            value = datetime.strptime(
                f"{date_match.group(1)} {matches[-1].group(1)}",
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError:
            return None
        return value.astimezone()

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
        heartbeat_at = self._business_heartbeat_at(path, text)
        heartbeat_age = (
            max(0.0, (at - heartbeat_at).total_seconds())
            if heartbeat_at is not None
            else None
        )
        fresh_limit = float(self.config.business_heartbeat_stale_seconds)
        if heartbeat_age is not None:
            business_healthy = active and not burst and heartbeat_age <= fresh_limit
            heartbeat_source = "explicit_business_success"
        else:
            # Compatibility fallback for older Rocket versions that do not emit
            # the structured success marker. A fresh log remains conservative:
            # it blocks an automatic QMT restart while Rocket is visibly active.
            business_healthy = active and not burst and age <= fresh_limit
            heartbeat_age = age if active else None
            heartbeat_source = "log_freshness_fallback" if active else "none"
        reason = (
            f"Rocket account-query error burst detected ({matches} samples)"
            if burst
            else "Rocket进程与业务心跳正常"
            if business_healthy
            else "Rocket进程存在，但业务心跳已过期"
            if active
            else "Rocket当前未运行"
        )
        return RocketObservation(
            active,
            burst,
            reason,
            age,
            business_healthy,
            heartbeat_age,
            heartbeat_source,
            True,
        )
