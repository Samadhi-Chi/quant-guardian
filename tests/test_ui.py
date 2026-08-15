from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from quant_guardian.config import AppConfig
from quant_guardian.domain.models import GuardianState, RecommendedAction
from quant_guardian.service import ServiceStatus
from quant_guardian.ui.design_system import STATE_DESCRIPTORS
from quant_guardian.ui.dialogs import ManualConfirmDialog
from quant_guardian.ui.main_window import MainWindow
from quant_guardian.ui.widgets import compute_trend_metrics, sample_from_status

BASE = datetime(2026, 8, 10, 13, 5, tzinfo=timezone(timedelta(hours=8)))


class FakeNotifications:
    def __init__(self) -> None:
        self.listeners = []

    def subscribe(self, listener) -> None:
        self.listeners.append(listener)


def component(component_id: str, state: str, reason: str, children=None, metrics=None):
    return {
        "id": component_id,
        "name": component_id,
        "state": state,
        "reason": reason,
        "observed_at": BASE.isoformat(),
        "priority": "high" if component_id.endswith(("data", "order")) else "normal",
        "metrics": metrics or {},
        "children": children or [],
    }


def make_status(
    state: GuardianState = GuardianState.HEALTHY,
    *,
    at: datetime = BASE,
    latency: float = 28,
    live: bool = False,
    trade_state: str = "healthy",
) -> ServiceStatus:
    action = {
        GuardianState.SUSPECT: RecommendedAction.WAIT,
        GuardianState.DEGRADED: RecommendedAction.RECOVER_QMT,
        GuardianState.RECOVERING: RecommendedAction.WAIT,
        GuardianState.VERIFYING: RecommendedAction.VERIFY,
        GuardianState.MANUAL_REQUIRED: RecommendedAction.REQUIRE_MANUAL,
        GuardianState.LOCKOUT: RecommendedAction.LOCKOUT,
        GuardianState.PAUSED: RecommendedAction.WAIT,
    }.get(state, RecommendedAction.NONE)
    healthy = state in {GuardianState.HEALTHY, GuardianState.STARTING, GuardianState.VERIFYING}
    qmt_state = "healthy" if healthy else "critical"
    qmt_children = [
        component("qmt_api.process", qmt_state, "XtMiniQmt.exe 与 miniquote.exe", metrics={"processes": [{"pid": 1234}]}),
        component("qmt_api.xtquant", qmt_state, "XTQuant连接正常", metrics={"latency_ms": latency}),
        component("qmt_api.account", qmt_state, "账户只读查询正常", metrics={"latency_ms": latency}),
        component("qmt_api.trading_snapshot", "healthy", "业务汇总完成", metrics={"orders": 5, "trades": 2, "positions": 3}),
    ]
    trade_children = [
        component("trade_system.data", "healthy", "Fuel数据新鲜", metrics={"products": 41, "errors": 0}),
        component(
            "trade_system.selection",
            trade_state,
            "当前使用Zeus：Zeus选股状态",
            metrics={"engine": "Zeus", "selected_engine": "zeus"},
        ),
        component("trade_system.order", "healthy", "Rocket下单状态", metrics={"engine": "Rocket"}),
    ]
    attention_required = state not in {GuardianState.HEALTHY, GuardianState.STARTING}
    if state is GuardianState.HEALTHY and trade_state in {"warning", "critical"}:
        attention_required = True
    target = "restart" if state is GuardianState.DEGRADED else "manual" if state is GuardianState.MANUAL_REQUIRED else "unlock" if state is GuardianState.LOCKOUT else "monitor" if trade_state in {"warning", "critical"} else "check"
    return ServiceStatus(
        state=state,
        action=action,
        reason=f"{state.value} test reason",
        observed_at=at,
        safety_live_actions=live,
        safety_reason="观察模式未创建恢复授权哨兵" if not live else "恢复配置与安全哨兵均有效",
        process={
            "status": "healthy" if healthy else "unresponsive",
            "reason": "QMT process evidence",
            "processes": [{"pid": 1234, "name": "XtMiniQmt.exe", "responsive": healthy}],
        },
        probe={
            "status": "healthy" if healthy else "timeout",
            "reason": "read-only probe evidence",
            "latency_ms": latency,
            "account_status": "connected",
            "account_ref": "SHOULD-NOT-RENDER-FULL-ACCOUNT",
        },
        log={"signal": "positive" if healthy else "stale", "reason": "log evidence"},
        rocket={"active": True, "reason": "Rocket process detected", "log_age_seconds": 4},
        reconciliation={
            "reason": "需要核对实盘",
            "orders": 5,
            "cancelable_orders": 1,
            "trades": 2,
            "positions": 3,
        },
        components={
            "qmt_api": component("qmt_api", qmt_state, "QMT API状态", qmt_children),
            "trade_system": component("trade_system", trade_state, "Trade System状态", trade_children),
        },
        attention={
            "required": attention_required,
            "level": "critical" if state in {GuardianState.DEGRADED, GuardianState.MANUAL_REQUIRED, GuardianState.LOCKOUT} or trade_state == "critical" else "warning" if attention_required else "success",
            "title": "Trade System选股内核需要处理" if trade_state in {"warning", "critical"} and state is GuardianState.HEALTHY else "QMT API需要处理" if attention_required else "当前无需操作",
            "message": "查看Zeus错误证据" if trade_state in {"warning", "critical"} else "所有关键检查均正常",
            "action": "查看监控" if target == "monitor" else "受控重启" if target == "restart" else "核对并确认" if target == "manual" else "解除恢复锁定" if target == "unlock" else "立即检测",
            "target": target,
        },
        schedule={
            "mode": "active",
            "interval_seconds": 5.0,
            "next_check_at": (at + timedelta(seconds=5)).isoformat(),
            "calendar_source": "builtin_2026",
        },
    )


class FakeService:
    def __init__(self, status: ServiceStatus | None = None) -> None:
        self.status = status or make_status()
        self.notifications = FakeNotifications()
        self.listeners = []
        self.calls: list[str] = []
        self.events = [
            {
                "time": "2026-08-10T13:05:00+08:00",
                "event_id": "evt-001",
                "event_type": "state_transition",
                "severity": "warning",
                "component_id": "qmt_api",
                "summary": "probe timeout",
                "payload": {"new_state": "suspect", "reason": "probe timeout"},
            }
        ]

    def subscribe(self, listener) -> None:
        self.listeners.append(listener)

    def query_events(self, *, limit=200, offset=0, search="", severity="all", component="all", since=None, until=None):
        rows = self.events
        if search:
            rows = [row for row in rows if search.casefold() in json.dumps(row, ensure_ascii=False).casefold()]
        if severity != "all":
            rows = [row for row in rows if row["severity"] == severity]
        if component != "all":
            rows = [row for row in rows if str(row.get("component_id", "")).startswith(component)]
        return rows[offset : offset + limit]

    def trend_samples(self, *, since, limit=None):
        return []

    def request_check(self):
        self.calls.append("request_check")

    def run_once(self):
        self.calls.append("run_once")
        return self.status

    def operator_check(self, source="all"):
        self.calls.append(f"operator_check:{source}")
        return self.status

    def manual_restart(self, *, operator_confirmed=False):
        self.calls.append("manual_restart")
        self.calls.append(f"qmt_confirmed:{operator_confirmed}")
        return self.status

    def manual_restart_trade_system(self, *, operator_confirmed=False):
        self.calls.append("manual_restart_trade_system")
        self.calls.append(f"trade_confirmed:{operator_confirmed}")
        return self.status

    def unlock(self):
        self.calls.append("unlock")
        return self.status

    def acknowledge_manual(self):
        self.calls.append("acknowledge_manual")
        return self.status

    def export_diagnostics(self, destination: Path):
        self.calls.append("export")
        return destination

    def stop(self):
        self.calls.append("stop")


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def make_window(self, status: ServiceStatus | None = None, path: Path | None = None) -> MainWindow:
        service = FakeService(status)
        config = AppConfig()
        window = MainWindow(service, config, path or Path("quant-guardian-test.json"), enable_tray=False)
        self.addCleanup(window.deleteLater)
        return window

    def test_shell_contains_three_pages_and_five_settings_sections(self) -> None:
        window = self.make_window()
        self.assertEqual(window.page_stack.count(), 3)
        self.assertEqual(window.settings_stack.count(), 5)
        self.assertEqual(len(window.nav_buttons), 3)
        self.assertEqual(len(window.settings_nav), 5)
        self.assertEqual(window.state_hero.title_label.text(), STATE_DESCRIPTORS[GuardianState.HEALTHY].title)
        self.assertEqual(window.qmt_check_button.text(), "检测")
        self.assertEqual(window.restart_qmt_button.text(), "重启")
        self.assertEqual(window.trade_check_button.text(), "检测")
        self.assertEqual(window.trade_restart_button.text(), "重启")
        self.assertFalse(hasattr(window, "attention_panel"))
        self.assertEqual(window.selection_engine.currentData(), "zeus")

    def test_every_guardian_state_has_a_complete_status_render(self) -> None:
        window = self.make_window(make_status(GuardianState.STARTING))
        for offset, state in enumerate(GuardianState):
            status = make_status(state, at=BASE + timedelta(seconds=offset))
            window.apply_status(status)
            self.assertEqual(window.state_hero.title_label.text(), STATE_DESCRIPTORS[state].title)
        window.apply_status(make_status(GuardianState.MANUAL_REQUIRED))
        self.assertFalse(window.state_hero.action_button.isHidden())
        self.assertEqual(window.state_hero.action_button.text(), "核对并确认")
        window.apply_status(make_status(GuardianState.LOCKOUT))
        self.assertFalse(window.state_hero.action_button.isHidden())
        self.assertEqual(window.state_hero.action_button.text(), "解除恢复锁定")

    def test_trade_system_alert_does_not_replace_qmt_state(self) -> None:
        window = self.make_window(make_status(trade_state="critical"))
        self.assertEqual(window.state_hero.title_label.text(), "Trade System选股内核需要处理")
        self.assertEqual(window.top_state_pill.text(), "需要处理")
        self.assertTrue(window.state_hero.action_button.isHidden())

    def test_closed_market_status_uses_neutral_winui_copy(self) -> None:
        status = make_status()
        status.schedule.update(
            {
                "mode": "idle",
                "interval_seconds": 3600.0,
                "trading_day": False,
            }
        )
        window = self.make_window(status)
        self.assertEqual(window.state_hero.kicker_label.text(), "休市监控")
        self.assertEqual(window.state_hero.title_label.text(), "今日休市，监控正常")
        self.assertEqual(window.state_hero.mode_pill.text(), "休市监控 · 1小时")
        self.assertEqual(window.top_state_pill.text(), "休市监控")
        self.assertEqual(window.top_state_pill.property("tone"), "neutral")

    def test_status_restart_button_bypasses_observe_mode_after_confirmation(self) -> None:
        window = self.make_window(make_status(live=False))
        with patch("quant_guardian.ui.main_window.RestartConfirmDialog") as dialog_cls, patch.object(
            window, "_run_service_operation"
        ) as run_operation:
            dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
            window.manual_restart()
        run_operation.assert_called_once()
        self.assertEqual(run_operation.call_args.args[0], "restart_qmt")
        run_operation.call_args.args[1]()
        self.assertIn("manual_restart", window.service.calls)
        self.assertIn("qmt_confirmed:True", window.service.calls)

    def test_trade_restart_uses_confirmation_and_operator_override(self) -> None:
        window = self.make_window(make_status(live=False))
        with patch("quant_guardian.ui.main_window.QuantclassRestartConfirmDialog") as dialog_cls, patch.object(
            window, "_run_service_operation"
        ) as run_operation:
            dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
            window.manual_restart_trade_system()
        run_operation.assert_called_once()
        self.assertEqual(run_operation.call_args.args[0], "restart_trade")
        run_operation.call_args.args[1]()
        self.assertIn("manual_restart_trade_system", window.service.calls)
        self.assertIn("trade_confirmed:True", window.service.calls)

    def test_sensitive_account_reference_is_not_rendered(self) -> None:
        window = self.make_window()
        rendered = " ".join(label.text() for label in window.findChildren(QLabel))
        self.assertNotIn("SHOULD-NOT-RENDER-FULL-ACCOUNT", rendered)

    def test_manual_confirmation_requires_explicit_checkbox(self) -> None:
        dialog = ManualConfirmDialog(make_status(GuardianState.MANUAL_REQUIRED))
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.ok_button.isEnabled())
        dialog.acknowledge.setChecked(True)
        self.assertTrue(dialog.ok_button.isEnabled())

    def test_monitor_charts_use_real_status_samples(self) -> None:
        window = self.make_window(make_status(at=BASE, latency=20))
        window.switch_page(1)
        window.apply_status(make_status(at=BASE + timedelta(minutes=1), latency=44))
        self.assertEqual(len(window.health_timeline.samples), 2)
        self.assertEqual(len(window.latency_chart.samples), 2)
        self.assertEqual(len(window.task_chart.samples), 2)
        self.assertNotEqual(window.latency_metric.value_label.text(), "—")
        self.assertIn("ms", window.latency_metric.value_label.text())

    def test_monitor_metrics_separate_qmt_availability_from_trade_failures(self) -> None:
        samples = [
            sample_from_status(
                make_status(
                    GuardianState.STARTING,
                    at=BASE,
                    latency=68,
                    trade_state="critical",
                )
            ),
            sample_from_status(
                make_status(
                    at=BASE + timedelta(seconds=30),
                    latency=0,
                    trade_state="critical",
                )
            ),
        ]
        availability, average, p95, trade, incidents, _coverage = (
            compute_trend_metrics(samples, "1h")
        )
        self.assertEqual(availability, "100.0%")
        self.assertEqual(average, "34 ms")
        self.assertEqual(p95, "68 ms")
        self.assertEqual(trade, "0.0%")
        self.assertEqual(incidents, "1")

    def test_event_model_and_detail_drawer(self) -> None:
        window = self.make_window()
        window._event_generation = 1
        window._apply_events(1, True, window.service.events)
        self.assertEqual(window.event_model.rowCount(), 1)
        window._show_event_index(window.event_model.index(0, 0))
        self.assertEqual(window.event_detail_title.text(), "state_transition")
        self.assertIn("probe timeout", window.event_detail_evidence.toPlainText())
        window._apply_events(2, True, [])
        self.assertEqual(window.event_model.rowCount(), 1, "stale async results must be ignored")

    def test_operation_metrics_and_stable_verification_detail(self) -> None:
        window = self.make_window()
        operation = {
            "operation_id": "QGO-test",
            "incident_id": "QGI-test",
            "started_at": "2026-08-10T13:48:00+08:00",
            "completed_at": "2026-08-10T13:48:45+08:00",
            "operation_type": "qmt_restart",
            "initiator": "automatic",
            "target_component": "qmt_api",
            "context": "production",
            "status": "succeeded",
            "phase": "verification",
            "attempt_no": 2,
            "duration_ms": 45_000,
            "summary": "稳定验证完成",
            "payload": {},
        }
        stats = {
            "qmt_restart_attempts": 2,
            "qmt_verified_attempts": 1,
            "attempt_success_rate": 0.5,
            "recovery_incidents": 1,
            "resolved_incidents": 1,
            "recovery_success_rate": 1.0,
            "repeated_incidents": 1,
            "blocked_operations": 0,
            "automatic_attempts": 2,
            "manual_attempts": 0,
            "median_mttr_ms": 45_000,
            "p95_mttr_ms": 45_000,
        }
        window._operations_generation = 1
        window._apply_operations(
            1,
            True,
            {"rows": [operation], "markers": [operation], "stats": stats},
        )
        self.assertEqual(window.operation_model.rowCount(), 1)
        self.assertEqual(window.recovery_success_metric.value_label.text(), "100.0%")
        self.assertEqual(window.verified_restart_metric.value_label.text(), "1 / 2")
        window._operation_detail_generation = 1
        window._apply_operation_detail(
            1,
            {
                "operation": operation,
                "incident": {"incident_id": "QGI-test", "status": "resolved"},
                "events": [
                    {
                        "time": operation["completed_at"],
                        "event_type": "recovery_verified",
                        "summary": "stable health verification completed",
                    }
                ],
            },
        )
        self.assertEqual(window.operation_detail_status.text(), "验证成功")
        self.assertIn("稳定验证通过", window.operation_detail_steps.toPlainText())

    def test_save_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            window = self.make_window(path=path)
            window.active_interval.setValue(7.5)
            window.notification_dedupe.setValue(12)
            with patch.object(QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok):
                window.save_settings()
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 2)
            self.assertEqual(data["monitoring"]["active_interval_seconds"], 7.5)
            self.assertEqual(data["notifications"]["dedupe_minutes"], 12)


if __name__ == "__main__":
    unittest.main()
