from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import AppConfig
from quant_guardian.diagnostics.audit import AuditLogger
from quant_guardian.domain.components import ComponentNode, ComponentState
from quant_guardian.domain.models import (
    GuardianState,
    LogSignal,
    ProbeStatus,
    ProcessStatus,
)
from quant_guardian.monitors.trade_system_monitor import TradeSystemObservation
from quant_guardian.probe.supervisor import ProbeObservation
from quant_guardian.safety import SENTINEL_CONTENT, SafetyGate
from quant_guardian.service import GuardianService
from tests.helpers import (
    FakeLogMonitor,
    FakeNetworkMonitor,
    FakeProbe,
    FakeProcessMonitor,
    FakeQuantclassController,
    FakeRecovery,
    FakeRocketMonitor,
)

BASE = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


class HealthyTradeMonitor:
    def observe(self, now, *, rocket, active_window):
        data = ComponentNode(
            "trade_system.data",
            "数据内核",
            ComponentState.HEALTHY,
            "Fuel正常",
            now,
        )
        selection = ComponentNode(
            "trade_system.selection",
            "选股内核",
            ComponentState.HEALTHY,
            "Zeus正常",
            now,
        )
        order = ComponentNode(
            "trade_system.order",
            "下单内核",
            ComponentState.IDLE,
            "Rocket空闲",
            now,
        )
        parent = ComponentNode(
            "trade_system",
            "Trade System",
            ComponentState.HEALTHY,
            "关键内核状态正常",
            now,
            children=(data, selection, order),
        )
        return TradeSystemObservation(parent, data, selection, order)


class CriticalTradeMonitor:
    def observe(self, now, *, rocket, active_window):
        data = ComponentNode("trade_system.data", "数据内核", ComponentState.HEALTHY, "Fuel正常", now)
        zeus = ComponentNode(
            "trade_system.selection.zeus",
            "选股引擎 · Zeus",
            ComponentState.CRITICAL,
            "Zeus失败",
            now,
            metrics={"engine": "Zeus"},
        )
        selection = ComponentNode(
            "trade_system.selection",
            "选股内核",
            ComponentState.CRITICAL,
            "当前使用Zeus：Zeus失败",
            now,
            children=(zeus,),
        )
        order = ComponentNode(
            "trade_system.order",
            "下单内核",
            ComponentState.IDLE,
            "Rocket空闲",
            now,
            metrics={"engine": "Rocket"},
        )
        parent = ComponentNode(
            "trade_system",
            "Trade System",
            ComponentState.CRITICAL,
            "选股链路需要处理",
            now,
            children=(data, selection, order),
        )
        return TradeSystemObservation(parent, data, selection, order)


class TrackingBusinessProbe:
    def __init__(self) -> None:
        self.invalidated_at = None

    @property
    def latest(self):
        return {"status": "healthy", "reason": "cached", "latency_ms": 1}

    def maybe_request(self, now, *, active):
        return None

    def invalidate_after_recovery(self, now):
        self.invalidated_at = now

    def stop(self):
        return None


class LoginFailedProbe(FakeProbe):
    def health(self) -> ProbeObservation:
        return ProbeObservation(
            ProbeStatus.FAILED,
            "QMT account login status is not healthy",
            0,
            "login_failed",
            "******42",
        )


class TimeoutProbe(FakeProbe):
    def health(self) -> ProbeObservation:
        return ProbeObservation(
            ProbeStatus.TIMEOUT,
            "read-only probe exceeded 5 seconds",
        )


class ServiceTests(unittest.TestCase):
    def make_config(self) -> AppConfig:
        config = AppConfig()
        config.thresholds.startup_grace_seconds = 0
        config.thresholds.verify_successes = 2
        config.thresholds.verify_min_span_seconds = 1
        config.recovery.backoff_seconds = [0, 0, 0]
        return config

    def test_healthy_service_reaches_healthy_after_stable_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=FakeRecovery(),
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            first = service.run_once(BASE)
            status = service.run_once(BASE + timedelta(seconds=1))
            self.assertEqual(first.schedule["interval_seconds"], 15.0)
            self.assertEqual(first.schedule["burst_reason"], "startup_verification")
            self.assertEqual(status.state, GuardianState.HEALTHY)
            self.assertEqual(status.schedule["interval_seconds"], 3600.0)
            service.stop()

    def test_closed_market_login_failure_is_idle_and_hourly(self) -> None:
        closed_moments = (
            datetime(2026, 8, 15, 10, 0, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 10, 1, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        for moment in closed_moments:
            with (
                self.subTest(moment=moment),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {"LOCALAPPDATA": directory}),
            ):
                config = self.make_config()
                config.mode = "recover"
                sentinel = Path(directory) / "RECOVERY_ENABLED"
                sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
                recovery = FakeRecovery()
                service = GuardianService(
                    config,
                    process_monitor=FakeProcessMonitor(),
                    log_monitor=FakeLogMonitor(LogSignal.EXPLICIT_DISCONNECT),
                    network_monitor=FakeNetworkMonitor(),
                    rocket_monitor=FakeRocketMonitor(),
                    trade_system_monitor=HealthyTradeMonitor(),
                    probe=LoginFailedProbe(),
                    recovery=recovery,
                    audit=AuditLogger(Path(directory) / "logs"),
                    safety_gate=SafetyGate(config, sentinel),
                    now=moment,
                )
                try:
                    service.run_once(moment)
                    status = service.run_once(moment + timedelta(seconds=1))
                    qmt = status.components["qmt_api"]
                    children = {child["id"]: child for child in qmt["children"]}
                    self.assertEqual(status.state, GuardianState.HEALTHY)
                    self.assertEqual(qmt["state"], "idle")
                    self.assertEqual(children["qmt_api.process"]["state"], "healthy")
                    self.assertEqual(children["qmt_api.xtquant"]["state"], "idle")
                    self.assertEqual(children["qmt_api.account"]["state"], "idle")
                    self.assertEqual(
                        children["qmt_api.trading_snapshot"]["state"], "idle"
                    )
                    self.assertFalse(status.attention["required"])
                    self.assertEqual(status.attention["title"], "今日休市，无需操作")
                    self.assertEqual(status.schedule["interval_seconds"], 3600.0)
                    self.assertFalse(
                        status.schedule["anomaly_confirmation"]["active"]
                    )
                    self.assertEqual(recovery.calls, 0)
                finally:
                    service.stop()

    def test_non_trading_day_process_failure_is_not_masked_or_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            moment = datetime(
                2026, 8, 15, 10, 0, tzinfo=timezone(timedelta(hours=8))
            )
            config = self.make_config()
            config.mode = "recover"
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            recovery = FakeRecovery()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(ProcessStatus.MISSING),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=recovery,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, sentinel),
                now=moment,
            )
            try:
                first = service.run_once(moment)
                second = service.run_once(moment + timedelta(seconds=15))
                confirmed = service.run_once(moment + timedelta(seconds=30))
                self.assertEqual(first.schedule["interval_seconds"], 15.0)
                self.assertEqual(second.schedule["interval_seconds"], 15.0)
                self.assertEqual(confirmed.state, GuardianState.DEGRADED)
                self.assertEqual(
                    confirmed.components["qmt_api"]["state"], "critical"
                )
                self.assertTrue(confirmed.attention["required"])
                self.assertEqual(confirmed.schedule["interval_seconds"], 3600.0)
                self.assertFalse(
                    confirmed.schedule["anomaly_confirmation"]["active"]
                )
                self.assertEqual(recovery.calls, 0)

                next_hour = service.run_once(moment + timedelta(hours=1))
                self.assertEqual(next_hour.schedule["interval_seconds"], 15.0)
                self.assertEqual(
                    next_hour.schedule["anomaly_confirmation"]["current"], 1
                )
                self.assertEqual(recovery.calls, 0)
            finally:
                service.stop()

    def test_closed_market_probe_timeout_is_idle_and_never_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            moment = datetime(
                2026, 8, 15, 10, 0, tzinfo=timezone(timedelta(hours=8))
            )
            config = self.make_config()
            config.mode = "recover"
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            recovery = FakeRecovery()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(LogSignal.EXPLICIT_DISCONNECT),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                trade_system_monitor=HealthyTradeMonitor(),
                probe=TimeoutProbe(),
                recovery=recovery,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, sentinel),
                now=moment,
            )
            try:
                service.run_once(moment)
                status = service.run_once(moment + timedelta(seconds=1))
                qmt = status.components["qmt_api"]
                children = {child["id"]: child for child in qmt["children"]}
                self.assertEqual(status.state, GuardianState.HEALTHY)
                self.assertEqual(qmt["state"], "idle")
                self.assertEqual(children["qmt_api.process"]["state"], "healthy")
                self.assertEqual(children["qmt_api.xtquant"]["state"], "idle")
                self.assertEqual(children["qmt_api.account"]["state"], "idle")
                self.assertFalse(status.attention["required"])
                self.assertEqual(status.schedule["interval_seconds"], 3600.0)
                self.assertEqual(recovery.calls, 0)
            finally:
                service.stop()

    def test_next_trading_day_resumes_strict_qmt_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            weekend = datetime(
                2026, 8, 15, 10, 0, tzinfo=timezone(timedelta(hours=8))
            )
            monday_open = datetime(
                2026, 8, 17, 8, 30, tzinfo=timezone(timedelta(hours=8))
            )
            config = self.make_config()
            config.mode = "recover"
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            recovery = FakeRecovery()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(LogSignal.EXPLICIT_DISCONNECT),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=LoginFailedProbe(),
                recovery=recovery,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, sentinel),
                now=weekend,
            )
            try:
                service.run_once(weekend)
                service.run_once(weekend + timedelta(seconds=1))
                service.run_once(monday_open)
                service.run_once(monday_open + timedelta(seconds=5))
                status = service.run_once(monday_open + timedelta(seconds=10))
                self.assertEqual(status.state, GuardianState.VERIFYING)
                self.assertEqual(status.schedule["mode"], "active")
                self.assertEqual(recovery.calls, 1)
            finally:
                service.stop()

    def test_live_recovery_only_runs_with_exact_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            config.mode = "recover"
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            recovery = FakeRecovery()
            quantclass = FakeQuantclassController()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(ProcessStatus.MISSING),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=recovery,
                quantclass_controller=quantclass,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, sentinel),
                now=BASE,
            )
            business = TrackingBusinessProbe()
            service.business = business
            try:
                # 17:00 local time is outside the active window. The v2
                # scheduler requires three consistent 15-second confirmations
                # before an idle-period recovery is permitted.
                first = service.run_once(BASE + timedelta(seconds=1))
                second = service.run_once(BASE + timedelta(seconds=16))
                status = service.run_once(BASE + timedelta(seconds=31))
                self.assertEqual(recovery.calls, 1)
                self.assertEqual(quantclass.calls, 0)
                self.assertEqual(first.schedule["interval_seconds"], 15.0)
                self.assertEqual(second.schedule["interval_seconds"], 15.0)
                self.assertEqual(status.state, GuardianState.VERIFYING)
                self.assertIsNotNone(business.invalidated_at)
            finally:
                service.stop()

    def test_rocket_active_suppresses_automatic_qmt_and_quantclass_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            config.mode = "recover"
            config.recovery.allow_qmt_restart_while_rocket_active = False
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            recovery = FakeRecovery()
            quantclass = FakeQuantclassController()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(ProcessStatus.MISSING),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(active=True),
                probe=FakeProbe(),
                recovery=recovery,
                quantclass_controller=quantclass,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, sentinel),
                now=BASE,
            )
            try:
                first = service.run_once(BASE + timedelta(seconds=1))
                second = service.run_once(BASE + timedelta(seconds=16))
                status = service.run_once(BASE + timedelta(seconds=31))
                self.assertEqual(first.action.value, "wait")
                self.assertEqual(second.action.value, "wait")
                self.assertEqual(status.state, GuardianState.MANUAL_REQUIRED)
                self.assertEqual(status.action.value, "require_manual")
                self.assertIn("Rocket is active", status.reason)
                self.assertEqual(recovery.calls, 0)
                self.assertEqual(quantclass.calls, 0)
            finally:
                service.stop()

    def test_observation_mode_never_calls_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            recovery = FakeRecovery()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(ProcessStatus.MISSING),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=recovery,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            service.run_once(BASE + timedelta(seconds=1))
            self.assertEqual(recovery.calls, 0)
            service.stop()

    def test_operator_confirmed_qmt_restart_bypasses_observe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            recovery = FakeRecovery()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=recovery,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            try:
                service.run_once(BASE + timedelta(seconds=1))
                status = service.manual_restart(operator_confirmed=True)
                self.assertFalse(status.safety_live_actions)
                self.assertEqual(status.state, GuardianState.VERIFYING)
                self.assertEqual(recovery.calls, 0)
                self.assertEqual(recovery.manual_calls, 1)
                self.assertEqual(service.machine.recovery_attempt_count, 0)
                self.assertTrue(service._wake_event.is_set())
                event_types = {
                    event["event_type"] for event in service.audit.recent(20)
                }
                self.assertIn("manual_qmt_restart_requested", event_types)
                self.assertIn("manual_qmt_restart_result", event_types)
                with self.assertRaises(RuntimeError):
                    service.manual_restart(operator_confirmed=True)
                self.assertEqual(recovery.manual_calls, 1)
                rejected = next(
                    event
                    for event in service.audit.recent(20)
                    if event["event_type"] == "manual_qmt_restart_rejected"
                )
                self.assertEqual(rejected["payload"]["status"], "blocked")
                self.assertEqual(
                    rejected["payload"]["phase"], "exclusive_verification"
                )
            finally:
                service.stop()

    def test_manual_restart_requires_explicit_operator_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            recovery = FakeRecovery()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=recovery,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            try:
                with self.assertRaises(PermissionError):
                    service.manual_restart()
                self.assertEqual(recovery.manual_calls, 0)
            finally:
                service.stop()

    def test_remote_restart_rechecks_network_and_rocket_at_service_boundary(self) -> None:
        for network, rocket, expected in (
            (False, False, "network is unavailable"),
            (True, True, "Rocket is active"),
        ):
            with self.subTest(network=network, rocket=rocket), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"LOCALAPPDATA": directory}
            ):
                config = self.make_config()
                recovery = FakeRecovery()
                service = GuardianService(
                    config,
                    process_monitor=FakeProcessMonitor(),
                    log_monitor=FakeLogMonitor(),
                    network_monitor=FakeNetworkMonitor(network),
                    rocket_monitor=FakeRocketMonitor(rocket),
                    probe=FakeProbe(),
                    recovery=recovery,
                    audit=AuditLogger(Path(directory) / "logs"),
                    safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                    now=BASE,
                )
                try:
                    service.run_once(BASE + timedelta(seconds=1))
                    with self.assertRaisesRegex(PermissionError, expected):
                        service.manual_restart(
                            operator_confirmed=True,
                            initiator="remote_telegram",
                            remote_channel="telegram",
                            remote_request_id="QGR-TEST",
                        )
                    self.assertEqual(recovery.manual_calls, 0)
                finally:
                    service.stop()

    def test_operator_check_is_recorded_as_a_completed_read_only_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=FakeRecovery(),
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            try:
                service.operator_check("qmt")
                events = service.audit.recent(10)
                requested = next(
                    event
                    for event in events
                    if event["event_type"] == "manual_check_requested"
                )
                result = next(
                    event
                    for event in events
                    if event["event_type"] == "manual_check_result"
                )
                self.assertEqual(
                    requested["payload"]["operation_id"],
                    result["payload"]["operation_id"],
                )
                self.assertEqual(result["payload"]["status"], "succeeded")
                self.assertEqual(result["payload"]["source"], "qmt")
            finally:
                service.stop()

    def test_operator_confirmed_quantclass_restart_is_manual_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            controller = FakeQuantclassController()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=FakeRecovery(),
                quantclass_controller=controller,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            try:
                service.manual_restart_trade_system(operator_confirmed=True)
                self.assertEqual(controller.calls, 1)
                event_types = {
                    event["event_type"] for event in service.audit.recent(20)
                }
                self.assertIn("manual_quantclass_restart_requested", event_types)
                self.assertIn("manual_quantclass_restart_result", event_types)
            finally:
                service.stop()

    def test_trade_system_failure_never_triggers_qmt_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            config.mode = "recover"
            sentinel = Path(directory) / "RECOVERY_ENABLED"
            sentinel.write_text(SENTINEL_CONTENT, encoding="utf-8")
            recovery = FakeRecovery()
            quantclass = FakeQuantclassController()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                trade_system_monitor=CriticalTradeMonitor(),
                probe=FakeProbe(),
                recovery=recovery,
                quantclass_controller=quantclass,
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, sentinel),
                now=BASE,
            )
            try:
                service.run_once(BASE)
                status = service.run_once(BASE + timedelta(seconds=1))
                self.assertEqual(status.state, GuardianState.HEALTHY)
                self.assertEqual(status.components["trade_system"]["state"], "critical")
                self.assertEqual(status.attention["target"], "monitor")
                self.assertEqual(recovery.calls, 0)
                self.assertEqual(quantclass.calls, 0)
                trade_event = next(
                    event
                    for event in service.audit.recent(10)
                    if event["event_type"] == "trade_system_state"
                )
                self.assertIn("Zeus", trade_event["payload"]["summary"])
            finally:
                service.stop()

    def test_monitor_loop_audits_exception_and_retries_without_dying(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            config.monitoring.monitor_error_retry_seconds = 1
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=FakeRecovery(),
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            recovered = threading.Event()
            calls = 0

            def flaky_run_once():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("synthetic monitor failure")
                recovered.set()
                return service.status

            service.run_once = flaky_run_once  # type: ignore[method-assign]
            try:
                service.start()
                self.assertTrue(recovered.wait(3))
                self.assertTrue(service.monitor_thread_alive)
                self.assertGreaterEqual(calls, 2)
                events = service.audit.recent(20)
                error = next(
                    event
                    for event in events
                    if event["event_type"] == "monitor_loop_error"
                )
                self.assertEqual(error["payload"]["consecutive_errors"], 1)
                heartbeat = Path(directory) / "QuantGuardian" / "state" / "monitor-heartbeat.json"
                self.assertTrue(heartbeat.is_file())
            finally:
                service.stop()

    def test_watchdog_can_restart_an_unexpectedly_dead_monitor_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            config = self.make_config()
            service = GuardianService(
                config,
                process_monitor=FakeProcessMonitor(),
                log_monitor=FakeLogMonitor(),
                network_monitor=FakeNetworkMonitor(),
                rocket_monitor=FakeRocketMonitor(),
                probe=FakeProbe(),
                recovery=FakeRecovery(),
                audit=AuditLogger(Path(directory) / "logs"),
                safety_gate=SafetyGate(config, Path(directory) / "sentinel"),
                now=BASE,
            )
            first_exit = threading.Event()
            recovered = threading.Event()
            calls = 0

            def terminal_once_loop():
                nonlocal calls
                calls += 1
                if calls == 1:
                    first_exit.set()
                    return
                recovered.set()
                service._stop_event.wait(2)

            service._run_loop = terminal_once_loop  # type: ignore[method-assign]
            try:
                service.start()
                self.assertTrue(first_exit.wait(1))
                deadline = time.monotonic() + 1
                while service.monitor_thread_alive and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(service.monitor_thread_alive)
                self.assertTrue(service.ensure_monitoring())
                self.assertTrue(recovered.wait(1))
                self.assertTrue(service.monitor_thread_alive)
            finally:
                service.stop()


if __name__ == "__main__":
    unittest.main()
