from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class QmtConfig:
    launcher: str = r"C:\QMT\bin.x64\XtItClient.exe"
    working_directory: str = r"C:\QMT\config\tradingtime"
    userdata_directory: str = r"C:\QMT\userdata_mini"
    log_directory: str = r"C:\QMT\userdata_mini\log"
    process_names: list[str] = field(
        default_factory=lambda: ["XtMiniQmt.exe", "miniquote.exe"]
    )


@dataclass(slots=True)
class ProbeConfig:
    enabled: bool = True
    python_executable: str = ""
    xtquant_parent: str = ""
    session_id: int = 100_000_001
    timeout_seconds: float = 5.0
    account_id_protected: str = ""


@dataclass(slots=True)
class RocketConfig:
    """Deprecated v1 compatibility group.

    Runtime code reads ``trade_system``.  The group remains serialised for one
    release so older scripts and exported diagnostics keep working.
    """

    enabled: bool = True
    process_names: list[str] = field(
        default_factory=lambda: ["rocket.exe", "python.exe"]
    )
    log_directory: str = r"C:\Quantclass\data\real_trading\rocket\data\系统日志"
    business_heartbeat_stale_seconds: int = 120


@dataclass(slots=True)
class TradeSystemConfig:
    enabled: bool = True
    selection_engine: str = "zeus"
    data_root: str = r"C:\Quantclass\data"
    client_executable: str = r"C:\Quantclass\quantclass.exe"
    client_process_names: list[str] = field(
        default_factory=lambda: ["quantclass.exe"]
    )
    quantclass_config: str = r"C:\Quantclass\config.json"
    fuel_process_names: list[str] = field(default_factory=lambda: ["fuel.exe"])
    fuel_update_commands: list[str] = field(default_factory=lambda: ["all_data"])
    fuel_status_file: str = r"code\data\products-status.json"
    fuel_update_file: str = r"code\data\products-update.json"
    fuel_log_directory: str = r"code\data\log"
    aqua_process_names: list[str] = field(default_factory=lambda: ["aqua.exe"])
    aqua_log_file: str = r"real_trading\logs\aqua.log"
    zeus_process_names: list[str] = field(default_factory=lambda: ["zeus.exe"])
    zeus_log_file: str = r"real_trading\logs\zeus.log"
    rocket_process_names: list[str] = field(
        default_factory=lambda: ["rocket.exe", "python.exe"]
    )
    rocket_log_directory: str = (
        r"real_trading\rocket\data\系统日志"
    )
    locker_directory: str = r"real_trading\data\locker"
    data_overdue_grace_seconds: int = 900
    data_stall_confirmation_seconds: int = 300
    rocket_expected_start: str = "09:00"
    rocket_startup_grace_seconds: int = 300
    task_log_tail_bytes: int = 262_144


@dataclass(slots=True)
class MonitoringConfig:
    active_start: str = "08:30"
    active_end: str = "16:30"
    active_interval_seconds: float = 5.0
    idle_interval_seconds: float = 3600.0
    anomaly_retry_seconds: float = 15.0
    monitor_error_retry_seconds: float = 15.0
    anomaly_confirmation_checks: int = 3
    business_summary_interval_seconds: float = 60.0
    business_summary_timeout_seconds: float = 2.0
    business_summary_retry_seconds: float = 300.0
    allow_idle_recovery: bool = True
    max_chart_points: int = 1200


@dataclass(slots=True)
class ThresholdConfig:
    # Kept for v1 CLI/config compatibility.  The scheduler uses monitoring.*.
    poll_interval_seconds: float = 5.0
    failure_threshold: int = 3
    failure_window_seconds: int = 45
    startup_grace_seconds: int = 90
    resume_grace_seconds: int = 120
    verify_successes: int = 3
    verify_min_span_seconds: int = 30
    verification_timeout_seconds: int = 180
    log_stale_seconds: int = 30


@dataclass(slots=True)
class RecoveryConfig:
    graceful_close_seconds: int = 20
    backoff_seconds: list[int] = field(default_factory=lambda: [60, 120, 300, 600])
    max_attempts_per_30_minutes: int = 3
    max_attempts_per_day: int = 5
    allow_qmt_restart_while_rocket_active: bool = False
    require_manual_rocket_resume: bool = True
    automatic_recovery_until: str = ""


@dataclass(slots=True)
class TradingConfig:
    timezone: str = "Asia/Shanghai"
    premarket_start: str = "08:30"
    morning_start: str = "09:15"
    morning_end: str = "11:30"
    afternoon_start: str = "13:00"
    afternoon_end: str = "15:00"
    postmarket_end: str = "15:30"
    holidays: list[str] = field(default_factory=list)
    manual_closed_dates: list[str] = field(default_factory=list)
    manual_open_dates: list[str] = field(default_factory=list)
    market: str = "SH"


@dataclass(slots=True)
class NotificationConfig:
    desktop_enabled: bool = True
    sound_on_critical: bool = True
    dedupe_minutes: int = 10


@dataclass(slots=True)
class DiagnosticConfig:
    retention_days: int = 30
    include_raw_qmt_logs: bool = False
    sqlite_index_enabled: bool = True


@dataclass(slots=True)
class AppConfig:
    schema_version: int = 2
    mode: str = "observe"
    qmt: QmtConfig = field(default_factory=QmtConfig)
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    rocket: RocketConfig = field(default_factory=RocketConfig)
    trade_system: TradeSystemConfig = field(default_factory=TradeSystemConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    diagnostics: DiagnosticConfig = field(default_factory=DiagnosticConfig)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.schema_version != 2:
            errors.append(f"unsupported schema_version: {self.schema_version}")
        if self.mode not in {"observe", "recover"}:
            errors.append("mode must be 'observe' or 'recover'")
        if self.monitoring.active_interval_seconds < 1:
            errors.append("active_interval_seconds must be >= 1")
        if self.monitoring.idle_interval_seconds < 60:
            errors.append("idle_interval_seconds must be >= 60")
        if self.monitoring.anomaly_retry_seconds < 1:
            errors.append("anomaly_retry_seconds must be >= 1")
        if self.monitoring.monitor_error_retry_seconds < 1:
            errors.append("monitor_error_retry_seconds must be >= 1")
        if self.monitoring.anomaly_confirmation_checks < 2:
            errors.append("anomaly_confirmation_checks must be >= 2")
        if self.monitoring.business_summary_interval_seconds < 30:
            errors.append("business_summary_interval_seconds must be >= 30")
        if self.monitoring.business_summary_timeout_seconds < 0.5:
            errors.append("business_summary_timeout_seconds must be >= 0.5")
        if self.thresholds.failure_threshold < 2:
            errors.append("failure_threshold must be >= 2")
        if self.thresholds.startup_grace_seconds < 30:
            errors.append("startup_grace_seconds must be >= 30")
        if self.thresholds.verify_successes < 2:
            errors.append("verify_successes must be >= 2")
        if self.thresholds.verification_timeout_seconds < 60:
            errors.append("verification_timeout_seconds must be >= 60")
        if self.probe.timeout_seconds < 1:
            errors.append("probe timeout_seconds must be >= 1")
        if self.recovery.max_attempts_per_30_minutes < 1:
            errors.append("max_attempts_per_30_minutes must be >= 1")
        if self.recovery.max_attempts_per_day < 1:
            errors.append("max_attempts_per_day must be >= 1")
        if self.recovery.automatic_recovery_until:
            try:
                expires = datetime.fromisoformat(
                    self.recovery.automatic_recovery_until
                )
                if expires.tzinfo is None:
                    raise ValueError
            except ValueError:
                errors.append(
                    "automatic_recovery_until must be an ISO 8601 timestamp with timezone"
                )
        if not self.trade_system.data_root:
            errors.append("trade_system.data_root must not be empty")
        if not self.trade_system.client_executable:
            errors.append("trade_system.client_executable must not be empty")
        if not self.trade_system.client_process_names:
            errors.append("trade_system.client_process_names must not be empty")
        if str(self.trade_system.selection_engine).casefold() not in {"aqua", "zeus"}:
            errors.append("trade_system.selection_engine must be 'aqua' or 'zeus'")
        if self.rocket.business_heartbeat_stale_seconds < 30:
            errors.append("rocket.business_heartbeat_stale_seconds must be >= 30")
        if self.trade_system.data_stall_confirmation_seconds < 0:
            errors.append("trade_system.data_stall_confirmation_seconds must be >= 0")
        if self.trade_system.rocket_startup_grace_seconds < 0:
            errors.append("trade_system.rocket_startup_grace_seconds must be >= 0")
        for label, value in (
            ("active_start", self.monitoring.active_start),
            ("active_end", self.monitoring.active_end),
            ("premarket_start", self.trading.premarket_start),
            ("morning_start", self.trading.morning_start),
            ("morning_end", self.trading.morning_end),
            ("afternoon_start", self.trading.afternoon_start),
            ("afternoon_end", self.trading.afternoon_end),
            ("postmarket_end", self.trading.postmarket_end),
            ("rocket_expected_start", self.trade_system.rocket_expected_start),
        ):
            try:
                hour, minute = value.split(":", 1)
                if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                    raise ValueError
            except (AttributeError, ValueError):
                errors.append(f"{label} must use HH:mm")
        return errors


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "QuantGuardian"


def default_config_path() -> Path:
    return app_data_dir() / "config" / "quant-guardian.json"


def recovery_sentinel_path() -> Path:
    return app_data_dir() / "state" / "RECOVERY_ENABLED"


def _merge_dataclass(instance: Any, values: dict[str, Any]) -> Any:
    for key, value in values.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def _migrate_v1(raw: dict[str, Any]) -> dict[str, Any]:
    migrated = json.loads(json.dumps(raw))
    migrated["schema_version"] = 2
    thresholds = migrated.setdefault("thresholds", {})
    poll = float(thresholds.get("poll_interval_seconds", 5.0))
    migrated.setdefault(
        "monitoring",
        {
            "active_start": "08:30",
            "active_end": "16:30",
            "active_interval_seconds": poll,
            "idle_interval_seconds": 3600.0,
            "anomaly_retry_seconds": 15.0,
            "monitor_error_retry_seconds": 15.0,
            "anomaly_confirmation_checks": 3,
            "business_summary_interval_seconds": 60.0,
            "business_summary_timeout_seconds": 2.0,
            "business_summary_retry_seconds": 300.0,
            "allow_idle_recovery": True,
            "max_chart_points": 1200,
        },
    )
    trading = migrated.setdefault("trading", {})
    old_holidays = list(trading.get("holidays") or [])
    trading.setdefault("manual_closed_dates", old_holidays)
    trading.setdefault("manual_open_dates", [])
    trading.setdefault("market", "SH")
    rocket = migrated.get("rocket") or {}
    trade_system = migrated.setdefault("trade_system", {})
    if rocket:
        trade_system.setdefault("enabled", bool(rocket.get("enabled", True)))
        trade_system.setdefault(
            "rocket_process_names", list(rocket.get("process_names") or [])
        )
        log_directory = str(rocket.get("log_directory") or "")
        root = str(trade_system.get("data_root") or TradeSystemConfig().data_root)
        try:
            relative = str(Path(log_directory).relative_to(Path(root)))
        except (ValueError, OSError):
            relative = log_directory or TradeSystemConfig().rocket_log_directory
        trade_system.setdefault("rocket_log_directory", relative)
    diagnostics = migrated.setdefault("diagnostics", {})
    diagnostics.setdefault("sqlite_index_enabled", True)
    return migrated


def _write_config_document(document: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _discover_quantclass_data_root(path: Path) -> str:
    """Read the small client config and locate its canonical data root."""
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            candidate = value.get("all_data_path")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(document)


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    config = AppConfig()
    migrated_document: dict[str, Any] | None = None
    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("configuration root must be an object")
        version = int(raw.get("schema_version", 1))
        if version == 1:
            migrated_document = _migrate_v1(raw)
            raw = migrated_document
        _merge_dataclass(config, raw)
    if not config.probe.python_executable:
        private_python = app_data_dir() / "Python311" / "python.exe"
        if private_python.is_file():
            config.probe.python_executable = str(private_python)
    if not config.probe.xtquant_parent:
        private_xtquant = app_data_dir() / "XtQuant"
        if (private_xtquant / "xtquant").is_dir():
            config.probe.xtquant_parent = str(private_xtquant)
    discovered_root = _discover_quantclass_data_root(
        Path(config.trade_system.quantclass_config)
    )
    configured_root = Path(config.trade_system.data_root)
    default_root = Path(TradeSystemConfig().data_root)
    if discovered_root and (
        configured_root == default_root or not configured_root.is_dir()
    ):
        config.trade_system.data_root = discovered_root
    # Keep deprecated fields coherent for one compatibility release.
    config.thresholds.poll_interval_seconds = config.monitoring.active_interval_seconds
    config.rocket.enabled = config.trade_system.enabled
    config.rocket.process_names = list(config.trade_system.rocket_process_names)
    rocket_path = Path(config.trade_system.rocket_log_directory)
    config.rocket.log_directory = str(
        rocket_path
        if rocket_path.is_absolute()
        else Path(config.trade_system.data_root) / rocket_path
    )
    errors = config.validate()
    if errors:
        raise ValueError("invalid configuration: " + "; ".join(errors))
    if migrated_document is not None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_name(
            f"{config_path.stem}.v1-backup-{timestamp}{config_path.suffix}"
        )
        if not backup.exists():
            shutil.copy2(config_path, backup)
        _write_config_document(asdict(config), config_path)
    return config


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    config.schema_version = 2
    config.thresholds.poll_interval_seconds = config.monitoring.active_interval_seconds
    config.rocket.enabled = config.trade_system.enabled
    config.rocket.process_names = list(config.trade_system.rocket_process_names)
    rocket_path = Path(config.trade_system.rocket_log_directory)
    config.rocket.log_directory = str(
        rocket_path
        if rocket_path.is_absolute()
        else Path(config.trade_system.data_root) / rocket_path
    )
    errors = config.validate()
    if errors:
        raise ValueError("invalid configuration: " + "; ".join(errors))
    config_path = path or default_config_path()
    _write_config_document(asdict(config), config_path)
    return config_path


def ensure_runtime_directories() -> dict[str, Path]:
    root = app_data_dir()
    paths = {
        "root": root,
        "config": root / "config",
        "state": root / "state",
        "logs": root / "logs",
        "diagnostics": root / "diagnostics",
        "cache": root / "cache",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths
