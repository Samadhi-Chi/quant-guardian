from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from quant_guardian.config import AppConfig, save_config
from quant_guardian.diagnostics.store import MonitoringStore
from quant_guardian.monitors.process_monitor import ProcessIdentity
from quant_guardian.watchdog import (
    WatchdogAudit,
    guardian_processes,
    latest_sample,
    run_watchdog,
    sampling_overdue,
)

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def write_health(root, when, *, sample=None):
    (root / "state").mkdir(exist_ok=True)
    (root / "state" / "monitor-heartbeat.json").write_text(json.dumps({"last_successful_check_at": when.isoformat()}), encoding="utf-8")
    with sqlite3.connect(root / "monitoring.db") as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS health_samples(time TEXT, timestamp REAL)")
        connection.execute("INSERT INTO health_samples VALUES(?,?)", ((sample or when).isoformat(), (sample or when).timestamp()))


def test_hourly_schedule_does_not_look_stale_at_five_minutes(tmp_path):
    write_health(tmp_path, NOW - timedelta(minutes=5))
    assert not sampling_overdue(tmp_path, NOW, 3600)
    assert sampling_overdue(tmp_path, NOW, 5)


def test_fresh_index_disproves_a_stale_heartbeat(tmp_path):
    write_health(tmp_path, NOW - timedelta(hours=2), sample=NOW)
    assert not sampling_overdue(tmp_path, NOW, 5)


def test_broken_index_never_overrides_fresh_heartbeat(tmp_path):
    write_health(tmp_path, NOW)
    (tmp_path / "monitoring.db").write_bytes(b"not a database")
    assert latest_sample(tmp_path) is None
    assert not sampling_overdue(tmp_path, NOW, 5)


def test_unknown_heartbeat_is_overdue(tmp_path):
    assert sampling_overdue(tmp_path, NOW, 5)


def test_watchdog_refuses_other_app_executable(tmp_path):
    other = tmp_path / "XtItClient.exe"
    other.touch()
    with pytest.raises(ValueError, match="only accepts"):
        run_watchdog(tmp_path / "unused.json", other, root=tmp_path)


def test_watchdog_only_launches_guardian_and_verifies_sampling(tmp_path):
    executable = tmp_path / "Quant Guardian.exe"
    executable.touch()
    config_path = tmp_path / "config.json"
    save_config(AppConfig(), config_path)

    def launch(arguments, **kwargs):
        assert arguments == [str(executable), "--config", str(config_path)]
        write_health(tmp_path, datetime.now().astimezone() + timedelta(seconds=1))

    with patch("quant_guardian.watchdog.guardian_processes", return_value=()), patch("quant_guardian.watchdog.subprocess.Popen", side_effect=launch):
        result = run_watchdog(config_path, executable, root=tmp_path)
    assert result["status"] == "succeeded"
    assert result["action"] == "guardian_restart"


def test_first_stale_check_waits_for_confirmation(tmp_path):
    executable = tmp_path / "Quant Guardian.exe"
    executable.touch()
    config_path = tmp_path / "config.json"
    save_config(AppConfig(), config_path)
    item = ProcessIdentity(100, executable.name, str(executable), 123.0, True)
    with patch("quant_guardian.watchdog.guardian_processes", return_value=(item,)), patch("quant_guardian.watchdog.sampling_overdue", return_value=True), patch("quant_guardian.watchdog.terminate_exact") as terminate, patch("quant_guardian.watchdog.subprocess.Popen") as launch:
        result = run_watchdog(config_path, executable, root=tmp_path)
    assert result["status"] == "suspect"
    terminate.assert_not_called()
    launch.assert_not_called()


def test_watchdog_does_not_mistake_its_own_process_for_the_desktop(tmp_path):
    executable = tmp_path / "Quant Guardian.exe"
    info = {"name": executable.name, "exe": str(executable), "create_time": 1.0, "cmdline": [str(executable), "--watchdog"]}
    fake = SimpleNamespace(pid=999999, info=info)
    with patch("quant_guardian.watchdog.psutil.process_iter", return_value=[fake]):
        assert guardian_processes(executable) == ()


def test_unverifiable_guardian_identity_blocks_recovery(tmp_path):
    executable = tmp_path / "Quant Guardian.exe"
    fake = SimpleNamespace(pid=999999, info={"name": executable.name, "exe": None})
    with patch("quant_guardian.watchdog.psutil.process_iter", return_value=[fake]), patch("quant_guardian.watchdog._limited_process_path", return_value=""):
        with pytest.raises(PermissionError):
            guardian_processes(executable)


def test_external_watchdog_result_appears_without_restarting_or_scanning_ui(tmp_path):
    audit = WatchdogAudit(tmp_path / "logs")
    store = MonitoringStore(tmp_path / "monitoring.db", audit_directory=tmp_path / "logs")
    try:
        store.enqueue_event({"event_id": "existing", "time": NOW.isoformat(), "event_type": "test"})
        audit.record("guardian_watchdog_restart_result", {
            "component_id": "quant_guardian", "operation_id": "watchdog-test",
            "operation_type": "guardian_worker_restart", "initiator": "watchdog",
            "target_component": "quant_guardian", "status": "succeeded", "reason": "sampling restored",
        })
        deadline = time.monotonic() + 3
        rows = []
        while time.monotonic() < deadline:
            rows = store.fetch_operations(operation_type="guardian_worker_restart")
            if rows:
                break
            time.sleep(0.05)
        assert rows[0]["status"] == "succeeded"
    finally:
        store.close()
