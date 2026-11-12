from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from quant_guardian.config import AppConfig, QmtConfig, TradeSystemConfig
from quant_guardian.domain.models import (
    GuardianState,
    ProbeStatus,
    ProcessStatus,
    RecommendedAction,
)
from quant_guardian.domain.state_machine import GuardianStateMachine
from quant_guardian.integration import ClientConfigReader, ClientSnapshot, resolve_runtime
from quant_guardian.monitors.execution_monitor import execution_nodes
from quant_guardian.monitors.process_monitor import QmtProcessMonitor
from quant_guardian.monitors.rocket_monitor import RocketObservation
from quant_guardian.monitors.trade_system_monitor import IncrementalTaskLog, TradeSystemMonitor
from quant_guardian.service import GuardianService
from tests.helpers import (
    FakeLogMonitor,
    FakeNetworkMonitor,
    FakeProbe,
    FakeProcessMonitor,
    FakeRecovery,
    FakeRocketMonitor,
)
from tests.test_state_machine import BASE, policy, snapshot


def write_client(path, mode="planA", automatic=True, checked=True):
    path.write_text(json.dumps({
        "real_market_config": {"qmt_mode": mode, "qmt_path": str(path.parent / "broker"), "account_id": "PRIVATE_ACCOUNT"},
        "settings": {"api_key": "PRIVATE_TOKEN", "all_data_path": str(path.parent), "is_auto_launch_real_trading": False},
        "auto_real_trading": automatic, "qmt_connect_ok": checked,
        "pos_mgmt": {"strategies": [{"name": "new", "strategy_list": [{"name": "a"}]}]},
    }), encoding="utf-8")


def test_client_reader_excludes_credentials_and_retains_last_good_mode(tmp_path):
    path = tmp_path / "client.json"
    write_client(path)
    reader = ClientConfigReader(path)
    first = reader.read()
    assert first.mode == "planA" and first.automatic_live
    assert first.strategy_keys == frozenset({"X1-new#0.a"})
    assert "PRIVATE" not in str(asdict(first))
    path.write_text('{"real_market_config":', encoding="utf-8")
    broken = reader.read()
    assert not broken.valid and broken.mode == "planA"
    write_client(path, mode="planX")
    assert reader.read().mode == "planX"
    path.unlink()
    assert not reader.read().valid


@pytest.mark.parametrize("mode,adapter,process,userdata", [
    ("mini_qmt", "xtquant", "XtMiniQmt.exe", "userdata_mini"),
    ("planA", "broker_runtime", "XtItClient.exe", "userdata"),
    ("planX", "manual_plan", "XtItClient.exe", "userdata"),
    ("qmt", "unavailable", "XtItClient.exe", "userdata"),
])
def test_runtime_profiles_do_not_rewrite_saved_config(tmp_path, mode, adapter, process, userdata):
    config = AppConfig()
    before = asdict(config)
    result = resolve_runtime(config, ClientSnapshot(mode=mode, qmt_root=str(tmp_path)))
    assert result.adapter == adapter
    assert result.config.qmt.main_process_name == process
    assert Path(result.config.qmt.userdata_directory).name == userdata
    assert asdict(config) == before
    if mode == "planA":
        assert result.config.probe.python_executable == str(tmp_path / "bin.x64" / "pythonw.exe")
        assert "site-packages" in result.config.probe.xtquant_parent


def test_unknown_mode_never_falls_back_to_mini(tmp_path):
    path = tmp_path / "client.json"
    write_client(path, mode="new-unrecognized-mode")
    profile = resolve_runtime(AppConfig(), ClientConfigReader(path).read())
    assert profile.mode == "unknown" and not profile.automatic_recovery_supported


@pytest.mark.parametrize("status", [ProbeStatus.CAPABILITY_DISABLED, ProbeStatus.PERMISSION_REQUIRED, ProbeStatus.UNAVAILABLE])
def test_unrecoverable_api_failures_cannot_request_restart(status):
    machine = GuardianStateMachine(policy(), now=BASE)
    for second in (0, 15, 30, 45, 60):
        result = machine.observe(snapshot(second, probe=status), recovery_permitted=True)
        assert result.action is RecommendedAction.REQUIRE_MANUAL
    assert machine.recovery_attempt_count == 0


def test_mode_switch_discards_failure_window_without_resetting_lockout():
    machine = GuardianStateMachine(policy(), now=BASE)
    machine.state = GuardianState.LOCKOUT
    machine._attempt_times.append(BASE)
    machine.reset_evidence(BASE + timedelta(seconds=10))
    assert machine.state is GuardianState.LOCKOUT
    assert machine.recovery_attempt_count == 1


def test_elevated_process_without_readable_path_is_unknown_not_missing(tmp_path):
    config = QmtConfig(launcher=str(tmp_path / "XtItClient.exe"), main_process_name="XtItClient.exe", process_names=["XtItClient.exe"])
    process = type("Process", (), {"info": {"pid": 123, "name": "XtItClient.exe", "exe": None}})()
    with patch("quant_guardian.monitors.process_monitor.psutil.process_iter", return_value=[process]), patch("quant_guardian.monitors.process_monitor._limited_process_path", return_value=""):
        assert QmtProcessMonitor(config).observe().status is ProcessStatus.UNKNOWN


def test_full_qmt_uses_configured_main_process(tmp_path):
    config = QmtConfig(launcher=str(tmp_path / "XtItClient.exe"), main_process_name="XtItClient.exe", process_names=["XtItClient.exe"])
    process = type("Process", (), {"info": {"pid": 123, "name": "XtItClient.exe", "exe": str(tmp_path / "XtItClient.exe"), "create_time": 1.0}})()
    with patch("quant_guardian.monitors.process_monitor.psutil.process_iter", return_value=[process]), patch("quant_guardian.monitors.process_monitor._window_responsive", return_value=True):
        assert QmtProcessMonitor(config).observe().status is ProcessStatus.HEALTHY


@pytest.mark.parametrize("mode,automatic,checked,condition", [
    ("planA", False, True, "operator_disabled"),
    ("planA", True, False, "client_selfcheck_failed"),
    ("planX", True, True, "ready"),
])
def test_execution_readiness_follows_actual_mode(tmp_path, mode, automatic, checked, condition):
    path = tmp_path / "client.json"
    write_client(path, mode, automatic, checked)
    config = TradeSystemConfig(data_root=str(tmp_path), quantclass_config=str(path))
    monitor = TradeSystemMonitor(config)
    now = datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    with patch.object(monitor, "_active_processes", return_value=[{"pid": 1}]):
        result = monitor.observe(now, rocket=RocketObservation(False, False, "idle"), active_window=True)
    readiness = next(child for child in result.node.children if child.id == "trade_system.readiness")
    assert readiness.metrics["condition"] == condition
    if not automatic or mode == "planX":
        assert result.order.state.value == "idle"
    if mode == "planX":
        plan = next(child for child in result.node.children if child.id == "trade_system.plan")
        assert plan.metrics["condition"] == "missing_required_plan"
        assert plan.state.value == "critical"


def test_optional_plan_failure_does_not_mark_selection_failed(tmp_path):
    path = tmp_path / "fusion.log"
    path.write_text(
        "2026-11-12 10:00:00 - [fusion] pid 1 start\n"
        "2026-11-12 10:00:01 - [ERROR] 生成目标占比调仓脚本失败\n"
        "Traceback (most recent call last):\nKeyError: 'accounts'\n"
        "2026-11-12 10:00:02 - [fusion] pid 1 exit successfully\n",
        encoding="utf-8",
    )
    runtime = IncrementalTaskLog(path).observe()
    assert runtime.plan_error_at and runtime.last_result == "success"


def test_plan_generation_is_never_execution_confirmation(tmp_path):
    config = TradeSystemConfig(data_root=str(tmp_path))
    plans = tmp_path / "real_trading" / "plans"
    plans.mkdir(parents=True)
    (plans / "plan_py36_20261112.py").write_text("# generated, not executed", encoding="utf-8")
    log = IncrementalTaskLog(tmp_path / "absent.log").runtime
    client = ClientSnapshot(available=True, mode="planX", automatic_live=True)
    now = datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    readiness, plan, handoff = execution_nodes(config, client, now, active_window=True, runtime=log, client_running=True)
    assert plan.metrics["output_count"] == 1
    assert not plan.metrics["execution_confirmed"]
    assert plan.state.value == "healthy"
    assert "未验证" in plan.reason


def test_handoff_warning_uses_strategy_ledger_without_claiming_broker_reconciliation(tmp_path):
    config = TradeSystemConfig(data_root=str(tmp_path))
    ledger = tmp_path / "real_trading" / "rocket" / "data" / "账户信息" / "实盘临时调仓信号-个股-仓位记录.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("策略名称,当前持仓量\nold#0.a,100\nnew#0.a,200\n", encoding="utf-8-sig")
    client = ClientSnapshot(available=True, mode="planA", strategy_keys=frozenset({"new#0.a"}))
    log = IncrementalTaskLog(tmp_path / "absent.log").runtime
    _, _, handoff = execution_nodes(config, client, BASE, active_window=True, runtime=log, client_running=True)
    assert handoff.metrics["legacy_shares"] == 100
    assert not handoff.metrics["broker_reconciled"]
    assert handoff.state.value == "warning"


def test_live_mode_switch_changes_contract_without_running_old_probe(tmp_path):
    path = tmp_path / "client.json"
    write_client(path, "planX")
    config = AppConfig()
    config.trade_system.quantclass_config = str(path)
    config.trade_system.data_root = str(tmp_path)
    recovery = FakeRecovery()
    probe = FakeProbe()
    service = GuardianService(
        config, runtime_root=tmp_path / "guardian", now=BASE,
        process_monitor=FakeProcessMonitor(), log_monitor=FakeLogMonitor(),
        network_monitor=FakeNetworkMonitor(), rocket_monitor=FakeRocketMonitor(),
        probe=probe, recovery=recovery,
    )
    try:
        first = service.run_once(BASE)
        assert first.integration["mode"] == "planX"
        assert first.probe["status"] == "not_applicable"
        write_client(path, "planA")
        second = service.run_once(BASE + timedelta(seconds=5))
        assert second.integration["mode"] == "planA"
        assert second.probe["status"] == "healthy"
    finally:
        service.stop()


def test_launch_privilege_warning_does_not_turn_healthy_api_into_an_outage(tmp_path):
    path = tmp_path / "client.json"
    write_client(path, "planA")
    config = AppConfig()
    config.trade_system.quantclass_config = str(path)
    config.trade_system.data_root = str(tmp_path)
    service = GuardianService(
        config, runtime_root=tmp_path / "guardian", now=BASE,
        process_monitor=FakeProcessMonitor(), log_monitor=FakeLogMonitor(),
        network_monitor=FakeNetworkMonitor(), rocket_monitor=FakeRocketMonitor(),
        probe=FakeProbe(), recovery=FakeRecovery(),
    )
    try:
        with patch("quant_guardian.service.launch_permission_problem", return_value="管理员权限未配置"):
            result = service.run_once(BASE + timedelta(minutes=1))
        assert result.components["qmt_api"]["state"] == "healthy"
        assert result.attention["title"] == "自动恢复需要配置运行权限"
        assert result.integration["launch_permission_problem"]
    finally:
        service.stop()
