"""External Guardian watchdog. It never starts, stops or probes trading apps."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psutil

from quant_guardian.config import app_data_dir, load_config
from quant_guardian.diagnostics.audit import AuditLogger
from quant_guardian.domain.trading_calendar import TradingCalendar
from quant_guardian.monitors.process_monitor import ProcessIdentity, _limited_process_path
from quant_guardian.recovery.windows_process_control import terminate_exact, wait_for_exit


class WatchdogAudit(AuditLogger):
    def _path_for(self, moment: datetime) -> Path:
        return self.log_directory / f"guardian-watchdog-{moment:%Y%m%d}.jsonl"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def latest_sample(root: Path) -> datetime | None:
    try:
        with sqlite3.connect((root / "monitoring.db").as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
            row = connection.execute("SELECT time FROM health_samples ORDER BY timestamp DESC LIMIT 1").fetchone()
        value = datetime.fromisoformat(str(row[0])) if row else None
        return value if value is not None and value.tzinfo is not None else None
    except (OSError, sqlite3.Error, ValueError):
        return None


def sampling_overdue(root: Path, now: datetime, interval: float) -> bool:
    heartbeat = _read_json(root / "state" / "monitor-heartbeat.json")
    try:
        sampled = datetime.fromisoformat(str(heartbeat.get("last_successful_check_at")))
        if sampled.tzinfo is None:
            return True
    except (ValueError, TypeError):
        return True
    grace = max(90, float(interval) * 0.1)
    deadline = sampled + timedelta(seconds=interval + grace)
    if now <= deadline:
        return False
    indexed = latest_sample(root)
    # A broken index must not cause restarts while the monitor heartbeat is fresh;
    # a fresh index disproves a stale heartbeat file's claim that sampling stopped.
    return indexed is None or now > indexed + timedelta(seconds=interval + grace)


def guardian_processes(executable: Path) -> tuple[ProcessIdentity, ...]:
    identities = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time", "cmdline"]):
        try:
            info = process.info
            if process.pid == os.getpid() or str(info.get("name", "")).casefold() != executable.name.casefold():
                continue
            if "--watchdog" in (info.get("cmdline") or []):
                continue
            actual = info.get("exe") or _limited_process_path(process.pid)
            if not actual:
                raise PermissionError("Guardian进程身份无法验证")
            if actual and Path(actual).resolve() == executable.resolve():
                if not info.get("create_time"):
                    raise PermissionError("Guardian进程创建时间无法验证")
                identities.append(ProcessIdentity(process.pid, executable.name, str(actual), float(info.get("create_time") or 0), True))
        except PermissionError:
            raise
        except (psutil.Error, OSError):
            continue
    return tuple(identities)


def run_watchdog(config_path: Path, executable: Path, *, root: Path | None = None) -> dict:
    root = root or app_data_dir()
    # Enforce the single product executable; this entry point is not a generic
    # process restart facility and cannot be pointed at QMT or Quantclass.
    if executable.name != "Quant Guardian.exe" or not executable.is_file():
        raise ValueError("watchdog only accepts the installed Quant Guardian.exe")
    config = load_config(config_path)
    now = datetime.now().astimezone()
    interval = TradingCalendar(config.trading, config.monitoring).schedule_at(now).interval_seconds
    try:
        identities = guardian_processes(executable)
    except PermissionError:
        return {"status": "blocked", "action": "none", "reason": "Guardian进程身份无法验证"}
    if identities and not sampling_overdue(root, now, interval):
        return {"status": "healthy", "action": "none"}
    state_path = root / "state" / "watchdog.json"
    state = _read_json(state_path)
    if identities:
        try:
            since = datetime.fromisoformat(str(state.get("stale_since")))
            if since.tzinfo is None:
                since = now
        except (ValueError, TypeError):
            since = now
        signature = sorted((identity.pid, identity.create_time) for identity in identities)
        if state.get("identities") != [list(pair) for pair in signature]:
            since = now
        if (now - since).total_seconds() < 90:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"stale_since": since.isoformat(), "identities": signature}), encoding="utf-8")
            return {"status": "suspect", "action": "wait_for_confirmation"}
        terminate_exact(identities, executable.parent)
        if wait_for_exit(identities, timeout=5):
            return {"status": "blocked", "action": "none", "reason": "Guardian进程无法安全退出"}
    audit = WatchdogAudit(root / "logs", config.diagnostics.retention_days)
    operation_id = "QGO-watchdog-" + uuid.uuid4().hex[:8]
    operation = {
        "component_id": "quant_guardian", "operation_id": operation_id,
        "operation_type": "guardian_worker_restart", "initiator": "watchdog",
        "target_component": "quant_guardian", "context": "production", "started_at": now,
    }
    audit.record("guardian_watchdog_restart_requested", {
        **operation, "status": "in_progress", "reason": "Guardian进程缺失或心跳与样本持续过期",
    }, moment=now, event_id=operation_id, severity="warning")
    subprocess.Popen(
        [str(executable), "--config", str(config_path)], cwd=str(executable.parent),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + 20
    success = False
    while time.monotonic() < deadline:
        heartbeat = _read_json(root / "state" / "monitor-heartbeat.json")
        try:
            sampled = datetime.fromisoformat(str(heartbeat.get("last_successful_check_at")))
            indexed = latest_sample(root)
            success = sampled >= now and indexed is not None and indexed >= now
        except (TypeError, ValueError):
            success = False
        if success:
            break
        time.sleep(0.25)
    completed = datetime.now().astimezone()
    result = {
        "status": "succeeded" if success else "failed", "action": "guardian_restart",
        "reason": "Guardian已恢复真实采样" if success else "已启动Guardian，采样恢复尚未验证",
    }
    audit.record("guardian_watchdog_restart_result", {
        **operation, **result, "success": success, "completed_at": completed,
        "duration_ms": int((completed - now).total_seconds() * 1000),
    }, moment=completed, event_id=operation_id, severity="info" if success else "warning")
    return result
