from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from quant_guardian.gateway.config import (
    default_messaging_config_path,
    load_messaging_config,
)
from quant_guardian.gateway.privacy import safe_message_text
from quant_guardian.gateway.store import GatewayStore
from quant_guardian.notifications import Notification

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
class GatewayEventBridge:
    """Converts local notifications and selected audit events into durable outbox rows."""

    OPERATION_EVENTS = {
        "recovery_requested",
        "recovery_result",
        "recovery_verified",
        "recovery_verification_failed",
        "manual_qmt_restart_requested",
        "manual_qmt_restart_result",
        "manual_qmt_restart_verified",
        "manual_qmt_restart_verification_failed",
        "manual_qmt_restart_rejected",
        "monitor_loop_error",
        "monitoring_gap",
        "monitor_thread_restarted",
        "gateway_control_server_failed",
        "gateway_process_start_failed",
    }

    def __init__(
        self,
        store: GatewayStore,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.store = store
        self.config_path = config_path or default_messaging_config_path()

    def _targets(self) -> list[tuple[str, str]]:
        try:
            config = load_messaging_config(self.config_path)
        except (OSError, ValueError):
            return []
        if not config.gateway_enabled or not config.broadcast.enabled:
            return []
        targets: list[tuple[str, str]] = []
        if config.telegram.enabled and config.telegram.home_chat_id:
            targets.append(("telegram", config.telegram.home_chat_id))
        if config.weixin.enabled and config.weixin.home_chat_id:
            targets.append(("weixin", config.weixin.home_chat_id))
        return targets

    def _allowed_severity(self, severity: str) -> bool:
        try:
            minimum = load_messaging_config(self.config_path).broadcast.minimum_severity
        except (OSError, ValueError):
            return False
        return _SEVERITY_RANK.get(severity, 0) >= _SEVERITY_RANK.get(minimum, 1)

    def on_notification(self, notification: Notification) -> None:
        try:
            config = load_messaging_config(self.config_path)
        except (OSError, ValueError):
            return
        key = notification.event_key
        if key in {"recovery_result", "manual_qmt_restart_result"}:
            # The audit callback carries the richer operation ID and final phase.
            return
        if key.startswith("trade_system:") or key in {
            "manual_required",
            "lockout",
            "healthy",
        }:
            if not config.broadcast.health_events:
                return
        elif key == "manual_quantclass_restart_result":
            if not config.broadcast.operation_events:
                return
        elif not config.broadcast.guardian_events:
            return
        if not self._allowed_severity(notification.severity):
            return
        text = (
            f"Quant Guardian · {safe_message_text(notification.title, 120)}\n"
            f"{safe_message_text(notification.message)}\n"
            f"时间：{notification.at.astimezone():%m-%d %H:%M:%S}"
        )
        for channel, chat_id in self._targets():
            self.store.enqueue_outbound(
                channel=channel,
                chat_id=chat_id,
                text=text,
                priority=20 if notification.severity == "critical" else 10,
                idempotency_key=(
                    f"notification:{channel}:{notification.event_key}:{notification.at.isoformat()}"
                ),
            )

    def on_audit(self, document: dict[str, Any]) -> None:
        event_type = str(document.get("event_type") or "")
        if event_type not in self.OPERATION_EVENTS:
            return
        try:
            config = load_messaging_config(self.config_path)
        except (OSError, ValueError):
            return
        is_recovery = event_type.startswith("recovery_")
        is_guardian = event_type in {
            "monitor_loop_error",
            "monitoring_gap",
            "monitor_thread_restarted",
            "gateway_control_server_failed",
            "gateway_process_start_failed",
        }
        is_operation = not is_recovery and not is_guardian
        if is_recovery and not config.broadcast.recovery_events:
            return
        if is_guardian and not config.broadcast.guardian_events:
            return
        if is_operation and not config.broadcast.operation_events:
            return
        payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
        severity = str(document.get("severity") or "info")
        # Successful launch/verification is operationally important even when
        # the general minimum severity is warning.
        successful_info = severity == "info" and event_type in {
            "recovery_result",
            "recovery_verified",
            "manual_qmt_restart_result",
            "manual_qmt_restart_verified",
        }
        if successful_info and not config.broadcast.include_healthy_recovery:
            return
        if not successful_info and not self._allowed_severity(severity):
            return
        event_id = str(document.get("event_id") or "")
        at_raw = str(document.get("time") or "")
        try:
            at = datetime.fromisoformat(at_raw).astimezone()
        except ValueError:
            at = datetime.now().astimezone()
        names = {
            "recovery_requested": "自动恢复已请求",
            "recovery_result": "自动恢复启动结果",
            "recovery_verified": "自动恢复验证成功",
            "recovery_verification_failed": "自动恢复验证失败",
            "manual_qmt_restart_requested": "QMT重启已请求",
            "manual_qmt_restart_result": "QMT重启启动结果",
            "manual_qmt_restart_verified": "QMT重启验证成功",
            "manual_qmt_restart_verification_failed": "QMT重启验证失败",
            "manual_qmt_restart_rejected": "QMT重启被阻断",
            "monitor_loop_error": "Guardian监控循环异常",
            "monitoring_gap": "Guardian监控采样中断",
            "monitor_thread_restarted": "Guardian监控线程已恢复",
            "gateway_control_server_failed": "远程控制本机接口启动失败",
            "gateway_process_start_failed": "消息Gateway启动失败",
        }
        initiator = str(payload.get("initiator") or "automatic")
        status = str(payload.get("status") or payload.get("result") or "")
        reason = safe_message_text(payload.get("reason"), 400)
        operation_id = str(payload.get("operation_id") or event_id)
        text = (
            f"Quant Guardian · {names.get(event_type, event_type)}\n"
            f"结果：{status or '已记录'}\n"
            f"发起：{safe_message_text(initiator, 60)}\n"
            f"说明：{reason or '请在监控页查看详情'}\n"
            f"操作编号：{safe_message_text(operation_id, 100)}\n"
            f"时间：{at:%m-%d %H:%M:%S}"
        )
        for channel, chat_id in self._targets():
            self.store.enqueue_outbound(
                channel=channel,
                chat_id=chat_id,
                text=text,
                priority=30 if severity == "critical" else 15,
                idempotency_key=f"audit:{channel}:{event_id}:{event_type}",
            )
