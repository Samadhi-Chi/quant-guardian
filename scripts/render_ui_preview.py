from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from quant_guardian.config import AppConfig
from quant_guardian.domain.models import GuardianState, RecommendedAction
from quant_guardian.gateway.config import MessagingConfig, save_messaging_config
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore
from quant_guardian.service import ServiceStatus
from quant_guardian.ui.design_system import install_ui_font
from quant_guardian.ui.main_window import MainWindow

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 8, 10, 13, 8, tzinfo=TZ)


class Notifications:
    def subscribe(self, _listener) -> None:
        return None


def node(
    component_id: str,
    name: str,
    state: str,
    reason: str,
    *,
    children: list[dict[str, object]] | None = None,
    metrics: dict[str, object] | None = None,
    priority: str = "normal",
) -> dict[str, object]:
    return {
        "id": component_id,
        "name": name,
        "state": state,
        "reason": reason,
        "observed_at": BASE.isoformat(),
        "priority": priority,
        "metrics": metrics or {},
        "children": children or [],
    }


def make_status(
    *,
    at: datetime = BASE,
    state: GuardianState = GuardianState.HEALTHY,
    qmt_state: str = "healthy",
    trade_state: str = "healthy",
    data_state: str = "healthy",
    selection_state: str = "healthy",
    latency: float = 31,
    live: bool = False,
    schedule_mode: str = "active",
) -> ServiceStatus:
    qmt_children = [
        node(
            "qmt_api.process",
            "QMT进程",
            qmt_state,
            "XtMiniQmt.exe、miniquote.exe 进程响应正常" if qmt_state == "healthy" else "XtMiniQmt.exe 进程缺失",
            metrics={"processes": [{"pid": 24948}, {"pid": 26212}] if qmt_state == "healthy" else []},
        ),
        node(
            "qmt_api.xtquant",
            "XTQuant连接",
            qmt_state,
            "XTQuant会话连接正常" if qmt_state == "healthy" else "连续三次无法建立XTQuant会话",
            metrics={"latency_ms": latency},
        ),
        node(
            "qmt_api.account",
            "账户只读查询",
            qmt_state,
            "账户登录且资产只读查询通过" if qmt_state == "healthy" else "账户查询超时",
            metrics={"latency_ms": latency},
        ),
        node(
            "qmt_api.trading_snapshot",
            "委托与持仓汇总",
            "healthy" if qmt_state == "healthy" else "pending",
            "独立会话四项汇总完成" if qmt_state == "healthy" else "等待QMT API恢复后重试",
            metrics={"orders": 12, "cancelable_orders": 2, "trades": 6, "positions": 4},
        ),
    ]
    zeus_state = "critical" if selection_state == "critical" else "idle"
    selection_children = [
        node(
            "trade_system.selection.aqua",
            "选股引擎 · Aqua",
            "idle",
            "Aqua未选用，最近一次任务成功",
            metrics={"engine": "Aqua", "selected": False, "last_result": "success"},
            priority="high",
        ),
        node(
            "trade_system.selection.zeus",
            "选股引擎 · Zeus",
            zeus_state,
            "策略所需数据字段缺失（Usecols不匹配）" if zeus_state == "critical" else "Zeus当前空闲，最近一次交易计划成功",
            metrics={"engine": "Zeus", "selected": True, "last_result": "failed" if zeus_state == "critical" else "success"},
            priority="high",
        ),
    ]
    trade_children = [
        node(
            "trade_system.data",
            "数据内核",
            data_state,
            "Fuel最近数据状态正常，共41项产品" if data_state == "healthy" else "2项数据产品超过计划更新时间",
            metrics={"products": 41, "errors": 0 if data_state == "healthy" else 2},
            priority="high",
        ),
        node(
            "trade_system.selection",
            "选股内核",
            selection_state,
            "当前使用Zeus：策略所需数据字段缺失（Usecols不匹配）" if selection_state == "critical" else "当前使用Zeus：最近一次选股任务成功",
            metrics={"engine": "Zeus", "selected_engine": "zeus"},
            children=selection_children,
            priority="high",
        ),
        node(
            "trade_system.order",
            "下单内核",
            "healthy",
            "Rocket进程与日志心跳正常",
            metrics={"engine": "Rocket", "active": True},
            priority="high",
        ),
    ]
    if state is GuardianState.DEGRADED:
        action = RecommendedAction.RECOVER_QMT
        reason = "QMT进程与XTQuant连接连续失败，已达到受控恢复阈值"
        attention = {
            "required": True,
            "level": "critical",
            "title": "QMT API故障，准备受控恢复",
            "message": "仅重启QMT；不会启动、停止或修复Trade System。",
            "action": "受控重启 QMT",
            "target": "restart",
        }
    elif selection_state == "critical":
        action = RecommendedAction.NONE
        reason = "QMT API运行健康；Trade System选股内核需要人工处理"
        attention = {
            "required": True,
            "level": "critical",
            "title": "Trade System选股内核需要处理",
            "message": "Zeus在18:16执行选股失败。Quant Guardian不会因此重启QMT。",
            "action": "查看监控证据",
            "target": "monitor",
        }
    else:
        action = RecommendedAction.NONE
        reason = "QMT API与Trade System关键检查均通过"
        attention = {
            "required": False,
            "level": "success",
            "title": "当前无需操作",
            "message": "QMT API、数据内核、Zeus选股和Rocket下单状态正常。",
            "action": "立即检测",
            "target": "check",
        }
    interval = 5.0 if schedule_mode == "active" else 3600.0
    return ServiceStatus(
        state=state,
        action=action,
        reason=reason,
        observed_at=at,
        safety_live_actions=live,
        safety_reason="恢复配置与安全哨兵均有效" if live else "观察模式：不会执行实时恢复动作",
        process={"status": qmt_state, "reason": qmt_children[0]["reason"], "processes": qmt_children[0]["metrics"].get("processes", [])},
        probe={"status": qmt_state, "reason": qmt_children[1]["reason"], "latency_ms": latency, "account_status": "connected" if qmt_state == "healthy" else "unknown"},
        log={"signal": "positive", "reason": "日志仅作为解释性证据"},
        rocket={"active": True, "reason": "Rocket进程与日志心跳正常", "log_age_seconds": 4},
        business_summary={"status": "healthy", "orders": 12, "cancelable_orders": 2, "trades": 6, "positions": 4},
        components={
            "qmt_api": node("qmt_api", "QMT API", qmt_state, reason, children=qmt_children),
            "trade_system": node("trade_system", "Trade System", trade_state, "数据、选股与下单三大内核状态已汇总", children=trade_children),
        },
        attention=attention,
        schedule={
            "mode": schedule_mode,
            "interval_seconds": interval,
            "trading_day": True,
            "calendar_source": "official-calendar",
            "calendar_uncertain": False,
            "next_check_at": (at + timedelta(seconds=interval)).isoformat(),
        },
    )


def make_events() -> list[dict[str, object]]:
    return [
        {
            "time": "2026-08-10T18:16:59+08:00",
            "event_id": "QG-20260810-ZEUS",
            "event_type": "trade_system_state",
            "severity": "critical",
            "component_id": "trade_system",
            "subcomponent_id": "trade_system.selection.zeus",
            "summary": "Zeus选股失败：策略所需数据字段缺失（Usecols不匹配）",
            "payload": {"engine": "Zeus", "last_result": "failed", "log_offset": 28410, "reason": "策略所需数据字段缺失（Usecols不匹配）"},
        },
        {
            "time": "2026-08-10T16:10:57+08:00",
            "event_id": "QG-20260810-FUEL",
            "event_type": "data_freshness",
            "severity": "info",
            "component_id": "trade_system.data",
            "summary": "Fuel数据状态正常，共41项产品",
            "payload": {"products": 41, "errors": 0},
        },
        {
            "time": "2026-08-10T15:01:12+08:00",
            "event_id": "QG-20260810-QMT",
            "event_type": "qmt_api_health",
            "severity": "info",
            "component_id": "qmt_api",
            "summary": "XTQuant与账户只读查询通过，P95 62ms",
            "payload": {"latency_ms": 43, "account_status": "connected"},
        },
        {
            "time": "2026-08-10T13:26:30+08:00",
            "event_id": "QG-20260810-ROCKET",
            "event_type": "rocket_heartbeat",
            "severity": "info",
            "component_id": "trade_system.order",
            "summary": "Rocket下单执行内核心跳正常",
            "payload": {"log_age_seconds": 4},
        },
        {
            "time": "2026-08-10T09:14:58+08:00",
            "event_id": "QG-20260810-CALENDAR",
            "event_type": "calendar_verified",
            "severity": "info",
            "component_id": "quant_guardian",
            "summary": "QMT交易日历交叉验证完成",
            "payload": {"market": "SH", "source": "qmt-calendar-cache"},
        },
    ]


def make_operations() -> list[dict[str, object]]:
    return [
        {
            "operation_id": "QGO-20260810-B",
            "incident_id": "QGI-20260810-1",
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
            "summary": "QMT API 已连续通过进程、XTQuant 与账户稳定验证",
            "payload": {"success": True},
        },
        {
            "operation_id": "QGO-20260810-A",
            "incident_id": "QGI-20260810-1",
            "started_at": "2026-08-10T13:45:00+08:00",
            "completed_at": "2026-08-10T13:47:30+08:00",
            "operation_type": "qmt_restart",
            "initiator": "automatic",
            "target_component": "qmt_api",
            "context": "production",
            "status": "failed",
            "phase": "verification",
            "attempt_no": 1,
            "duration_ms": 150_000,
            "summary": "QMT 启动后 XTQuant 未在验证时限内稳定恢复",
            "payload": {"success": False},
        },
        {
            "operation_id": "QGO-20260810-CHECK",
            "incident_id": "",
            "started_at": "2026-08-10T11:10:00+08:00",
            "completed_at": "2026-08-10T11:10:01+08:00",
            "operation_type": "manual_check",
            "initiator": "manual",
            "target_component": "qmt_api",
            "context": "production",
            "status": "succeeded",
            "phase": "completed",
            "attempt_no": 1,
            "duration_ms": 620,
            "summary": "QMT API 立即检测完成：healthy",
            "payload": {"observed_state": "healthy"},
        },
        {
            "operation_id": "QGO-20260810-WATCHDOG",
            "incident_id": "",
            "started_at": "2026-08-10T09:00:00+08:00",
            "completed_at": "2026-08-10T09:00:00+08:00",
            "operation_type": "guardian_worker_restart",
            "initiator": "watchdog",
            "target_component": "quant_guardian.monitor_loop",
            "context": "production",
            "status": "succeeded",
            "phase": "completed",
            "attempt_no": 1,
            "duration_ms": 12,
            "summary": "后台监控线程已恢复",
            "payload": {"success": True},
        },
    ]


class PreviewService:
    def __init__(self) -> None:
        self.status = make_status()
        self.notifications = Notifications()
        self.events = make_events()
        self.operations = make_operations()

    def subscribe(self, _listener) -> None:
        return None

    def query_events(self, *, limit=200, offset=0, search="", severity="all", component="all", since=None, until=None):
        rows = self.events
        if search:
            rows = [row for row in rows if search.casefold() in str(row).casefold()]
        if severity != "all":
            rows = [row for row in rows if row["severity"] == severity]
        if component != "all":
            rows = [row for row in rows if str(row.get("component_id", "")).startswith(component)]
        return rows[offset : offset + limit]

    def trend_samples(self, *, since, limit=None):
        return []

    def query_operations(
        self,
        *,
        limit=200,
        offset=0,
        operation_type="all",
        initiator="all",
        status="all",
        context="all",
        search="",
        **_kwargs,
    ):
        rows = self.operations
        if operation_type != "all":
            rows = [row for row in rows if row["operation_type"] == operation_type]
        if initiator != "all":
            rows = [row for row in rows if row["initiator"] == initiator]
        if status != "all":
            rows = [row for row in rows if row["status"] == status]
        if context != "all":
            rows = [row for row in rows if row["context"] == context]
        if search:
            rows = [row for row in rows if search.casefold() in str(row).casefold()]
        return rows[offset : offset + limit]

    def operation_stats(self, **_kwargs):
        return {
            "operations_total": 4,
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
            "median_mttr_ms": 225_000,
            "p95_mttr_ms": 225_000,
        }

    def operation_detail(self, operation_id):
        operation = next(
            row for row in self.operations if row["operation_id"] == operation_id
        )
        return {
            "operation": operation,
            "incident": {
                "incident_id": operation.get("incident_id"),
                "status": "resolved",
                "attempt_count": 2,
            }
            if operation.get("incident_id")
            else None,
            "events": [
                {
                    "time": operation["started_at"],
                    "event_type": "recovery_requested",
                    "summary": "连续故障已确认，开始受控恢复",
                },
                {
                    "time": operation["completed_at"],
                    "event_type": (
                        "recovery_verified"
                        if operation["status"] == "succeeded"
                        else "recovery_verification_failed"
                    ),
                    "summary": operation["summary"],
                },
            ],
        }

    def request_check(self):
        return None

    def operator_check(self, _source="all"):
        return self.status

    def record_settings_changed(self):
        return None

    def run_once(self):
        return self.status

    def manual_restart(self, *, operator_confirmed=False):
        return self.status

    def manual_restart_trade_system(self, *, operator_confirmed=False):
        return self.status

    def unlock(self):
        return self.status

    def acknowledge_manual(self):
        return self.status

    def export_diagnostics(self, destination: Path):
        return destination

    def stop(self):
        return None


def save_widget(application: QApplication, widget, path: Path) -> None:
    widget.show()
    for _index in range(3):
        application.processEvents()
    if not widget.grab().save(str(path), "PNG"):
        raise RuntimeError(f"failed to save {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    application.setFont(QFont(install_ui_font(), 10))
    config = AppConfig()
    service = PreviewService()
    messaging_path = args.output / "messaging.json"
    messaging = MessagingConfig(gateway_enabled=True)
    messaging.telegram.enabled = True
    messaging.telegram.allowed_user_ids = ["demo-owner"]
    messaging.telegram.home_chat_id = "demo-owner"
    messaging.weixin.enabled = True
    messaging.weixin.account_id = "demo@im.bot"
    messaging.weixin.allowed_user_ids = ["demo-owner"]
    messaging.weixin.home_chat_id = "demo-owner"
    messaging.remote_control.enabled = True
    save_messaging_config(messaging, messaging_path)
    preview_state_path = args.output / "state" / "gateway.db"
    for suffix in ("", "-wal", "-shm"):
        preview_state_path.with_name(preview_state_path.name + suffix).unlink(missing_ok=True)
    gateway_store = GatewayStore(preview_state_path)
    gateway_store.update_channel_state("telegram", "connected", identity="@quant_guardian_demo")
    gateway_store.update_channel_state("weixin", "connected", identity="demo@im.bot")
    gateway_store.record_command(
        request_id="QGR-DEMO-STATUS",
        channel="telegram",
        sender_id="demo-owner",
        chat_id="demo-owner",
        command="status",
        status="succeeded",
        reason="已返回脱敏状态摘要",
    )
    gateway_store.record_command(
        request_id="QGR-DEMO-RESTART",
        channel="weixin",
        sender_id="demo-owner",
        chat_id="demo-owner",
        command="restart_qmt",
        status="succeeded",
        reason="QMT受控重启已受理，正在验证",
        operation_id="QGO-DEMO-01",
    )
    gateway_store.enqueue_outbound(
        channel="telegram",
        chat_id="demo-owner",
        text="模拟告警",
        idempotency_key="preview:telegram:1",
    )
    outbound = gateway_store.claim_outbound("telegram")
    gateway_store.complete_outbound(outbound[0].message_id, success=True)
    vault = CredentialVault(
        args.output / "secrets" / "messaging-secrets.json",
        protect=lambda value: "preview:" + value[::-1],
        unprotect=lambda value: value.removeprefix("preview:")[::-1],
    )
    vault.set("telegram_bot_token", "synthetic-telegram-token")
    vault.set("weixin_bot_token", "synthetic-weixin-token")
    window = MainWindow(
        service,
        config,
        args.output / "preview-config.json",
        enable_tray=False,
        messaging_config_path=messaging_path,
        gateway_store=gateway_store,
        credential_vault=vault,
    )
    window.resize(1120, 820)

    healthy = make_status()
    window.switch_page(0)
    window.apply_status(healthy)
    save_widget(application, window, args.output / "01-home-trading-healthy.png")

    zeus = make_status(
        at=BASE.replace(hour=18, minute=18),
        trade_state="critical",
        selection_state="critical",
        schedule_mode="idle",
    )
    window.apply_status(zeus)
    save_widget(application, window, args.output / "02-home-zeus-attention.png")

    qmt_failure = make_status(
        at=BASE.replace(hour=18, minute=30),
        state=GuardianState.DEGRADED,
        qmt_state="critical",
        live=True,
        schedule_mode="idle",
    )
    window.apply_status(qmt_failure)
    save_widget(application, window, args.output / "03-home-qmt-controlled-recovery.png")

    window._history.clear()
    for index in range(120):
        at = BASE.replace(hour=8, minute=30) + timedelta(minutes=index * 5)
        selection = "critical" if 104 <= index <= 110 else "healthy"
        trade = "critical" if selection == "critical" else "healthy"
        status = make_status(
            at=at,
            latency=28 + (index % 11) * 4 + (65 if index in {32, 79} else 0),
            trade_state=trade,
            selection_state=selection,
        )
        window.apply_status(status)
    window.switch_page(1)
    window._event_generation += 1
    window._apply_events(window._event_generation, True, service.events)
    window._operations_generation += 1
    window._apply_operations(
        window._operations_generation,
        True,
        {
            "rows": service.operations,
            "markers": service.operations,
            "stats": service.operation_stats(),
        },
    )
    window._render_monitor_samples(list(window._history))
    window._gateway_generation += 1
    window._apply_gateway_data(
        window._gateway_generation,
        {
            "states": gateway_store.channel_states(),
            "stats": gateway_store.stats(since=BASE - timedelta(days=1)),
            "rows": gateway_store.activity(limit=20),
            "all_rows": gateway_store.activity(limit=20),
        },
    )
    save_widget(application, window, args.output / "04-monitor-today.png")

    window.monitoring_page.verticalScrollBar().setValue(575)
    application.processEvents()
    save_widget(application, window, args.output / "11-monitor-operations.png")

    window._selected_operation = service.operations[0]
    window._operation_detail_generation += 1
    window._apply_operation_detail(
        window._operation_detail_generation,
        service.operation_detail(service.operations[0]["operation_id"]),
    )
    application.processEvents()
    save_widget(application, window, args.output / "12-monitor-operation-detail.png")

    window._show_event_index(window.event_model.index(0, 0))
    window.event_raw_toggle.setChecked(True)
    application.processEvents()
    window.monitoring_page.verticalScrollBar().setValue(
        window.monitoring_page.verticalScrollBar().maximum()
    )
    save_widget(application, window, args.output / "05-monitor-event-drawer.png")

    window.switch_page(2)
    window._switch_settings(2)
    save_widget(application, window, args.output / "06-settings-frequency-calendar.png")
    window._switch_settings(1)
    save_widget(application, window, args.output / "07-settings-paths.png")
    window._switch_settings(0)
    save_widget(application, window, args.output / "08-settings-recovery-safety.png")
    window._switch_settings(5)
    save_widget(application, window, args.output / "13-settings-messaging.png")
    window._switch_settings(7)
    save_widget(application, window, args.output / "14-settings-remote-control.png")
    window._switch_settings(8)
    save_widget(application, window, args.output / "15-settings-messaging-audit.png")

    window.switch_page(0)
    window.apply_status(healthy)
    window.resize(1024, 768)
    save_widget(application, window, args.output / "09-home-1024x768.png")

    window.resize(1120, 820)
    window.toggle_theme()
    save_widget(application, window, args.output / "10-home-dark.png")
    window.deleteLater()
    application.processEvents()
    print(f"rendered 15 previews to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
