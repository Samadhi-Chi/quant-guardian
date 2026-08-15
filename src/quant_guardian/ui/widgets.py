from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from quant_guardian.domain.models import GuardianState, RecommendedAction
from quant_guardian.service import ServiceStatus
from quant_guardian.ui.design_system import (
    DARK,
    LIGHT,
    STATE_DESCRIPTORS,
    icon_pixmap,
    line_icon,
    nav_icon_size,
    set_dynamic_property,
)

SENSOR_STATUS_LABELS = {
    "unknown": "等待数据",
    "healthy": "健康",
    "missing": "未运行",
    "unresponsive": "无响应",
    "identity_mismatch": "身份不匹配",
    "failed": "检查失败",
    "timeout": "探针超时",
    "starting": "启动中",
    "unavailable": "不可用",
    "positive": "连接正常",
    "neutral": "无异常信号",
    "explicit_disconnect": "明确断开",
    "login_failure": "登录失败",
    "stale": "日志停滞",
    "warning": "需要关注",
    "critical": "异常",
    "idle": "空闲",
    "recovering": "恢复中",
    "pending": "等待汇总",
}


def sensor_label(raw: object) -> str:
    value = str(raw or "unknown")
    return SENSOR_STATUS_LABELS.get(value, value)


def sensor_tone(raw: object) -> str:
    value = str(raw or "unknown")
    if value in {"healthy", "positive"}:
        return "success"
    if value in {"starting", "recovering", "neutral", "unknown", "idle", "pending"}:
        return "info" if value == "starting" else "neutral"
    if value in {"stale", "timeout", "unavailable", "warning"}:
        return "warning"
    return "danger"


def _plain_reason(value: object, fallback: str = "等待下一次检测") -> str:
    text = str(value or "").strip()
    return text if text else fallback


class PillLabel(QLabel):
    def __init__(self, text: str = "", tone: str = "neutral", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("pill", "true")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        set_dynamic_property(self, "tone", tone)


class NavButton(QToolButton):
    def __init__(self, text: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navButton")
        self.setText(text)
        self.setIcon(line_icon(icon_name, 20))
        self.setIconSize(nav_icon_size())
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class SettingsNavButton(QToolButton):
    def __init__(self, text: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsNavButton")
        self.setText(text)
        self.setIcon(line_icon(icon_name, 19))
        self.setIconSize(nav_icon_size())
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class MetricCard(QFrame):
    def __init__(self, label: str, value: str = "—", caption: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(3)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        self.label_label = QLabel(label)
        self.label_label.setObjectName("metricLabel")
        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("cardCaption")
        self.caption_label.setWordWrap(True)
        layout.addWidget(self.value_label)
        layout.addWidget(self.label_label)
        if caption:
            layout.addWidget(self.caption_label)

    def set_value(self, value: str, caption: str | None = None) -> None:
        self.value_label.setText(value)
        if caption is not None:
            self.caption_label.setText(caption)
            self.caption_label.setVisible(bool(caption))


class StateHero(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("heroBanner")
        self._dark = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 17, 18, 17)
        layout.setSpacing(15)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(44, 44)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        copy = QVBoxLayout()
        copy.setSpacing(4)
        self.kicker_label = QLabel("启动验证")
        self.kicker_label.setObjectName("heroKicker")
        self.title_label = QLabel("正在建立健康基线")
        self.title_label.setObjectName("heroTitle")
        self.reason_label = QLabel("正在启动监控服务")
        self.reason_label.setObjectName("heroReason")
        self.reason_label.setWordWrap(True)
        copy.addWidget(self.kicker_label)
        copy.addWidget(self.title_label)
        copy.addWidget(self.reason_label)
        layout.addLayout(copy, 1)

        right = QVBoxLayout()
        right.setSpacing(5)
        self.mode_pill = PillLabel("安全观察", "neutral")
        self.action_button = QPushButton()
        self.action_button.setProperty("variant", "primary")
        self.action_button.setVisible(False)
        self.time_label = QLabel("最近检测 —")
        self.time_label.setObjectName("cardCaption")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.mode_pill, 0, Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignRight)
        right.addStretch()
        right.addWidget(self.time_label)
        layout.addLayout(right)

    def set_dark(self, dark: bool) -> None:
        self._dark = dark

    def update_status(self, status: ServiceStatus) -> None:
        descriptor = STATE_DESCRIPTORS[status.state]
        palette = DARK if self._dark else LIGHT
        attention = status.attention or {}
        schedule = status.schedule or {}
        market_closed_healthy = (
            schedule.get("trading_day") is False
            and status.state is GuardianState.HEALTHY
            and not attention.get("required")
        )
        trade_attention = bool(attention.get("required")) and str(
            attention.get("title") or ""
        ).startswith("Trade System")
        tone_key = {
            GuardianState.HEALTHY: "green",
            GuardianState.SUSPECT: "amber",
            GuardianState.PAUSED: "amber",
            GuardianState.DEGRADED: "red",
            GuardianState.MANUAL_REQUIRED: "red",
            GuardianState.LOCKOUT: "red",
        }.get(status.state, "blue")
        if trade_attention:
            tone_key = "red" if attention.get("level") == "critical" else "amber"
        elif market_closed_healthy:
            tone_key = "blue"
        accent = palette[tone_key]
        soft = palette[f"{tone_key}_soft"]
        border = palette["border"]
        self.kicker_label.setText(
            "需要处理"
            if trade_attention
            else "休市监控"
            if market_closed_healthy
            else descriptor.kicker.upper()
        )
        self.kicker_label.setStyleSheet(f"color: {accent};")
        self.title_label.setText(
            str(attention.get("title"))
            if trade_attention
            else "今日休市，监控正常"
            if market_closed_healthy
            else descriptor.title
        )
        self.reason_label.setText(
            _plain_reason(
                attention.get("message") if trade_attention else status.reason,
                "等待下一次检测",
            )
        )
        icon_name = (
            "warning"
            if trade_attention
            else "clock"
            if market_closed_healthy
            else descriptor.icon
        )
        self.icon_label.setPixmap(icon_pixmap(icon_name, accent, 26))
        self.icon_label.setStyleSheet(f"background: {soft}; border-radius: 10px;")
        interval = schedule.get("interval_seconds")
        if schedule.get("mode") == "active":
            interval_text = f"{float(interval or 5):g}秒"
            mode_text = f"活跃监控 · {interval_text}"
            mode_tone = "success"
        else:
            seconds = float(interval or 3600)
            interval_text = (
                f"{seconds / 60:g}分钟" if seconds < 3600 else f"{seconds / 3600:g}小时"
            )
            mode_text = (
                f"休市监控 · {interval_text}"
                if schedule.get("trading_day") is False
                else f"低频监控 · {interval_text}"
            )
            mode_tone = "neutral"
        self.mode_pill.setText(mode_text)
        self.mode_pill.set_tone(mode_tone)
        target = str(attention.get("target") or "")
        show_action = bool(attention.get("required")) and target in {
            "manual",
            "unlock",
        }
        self.action_button.setVisible(show_action)
        if show_action:
            self.action_button.setText(str(attention.get("action") or "处理"))
            self.action_button.setProperty("target", target)
        next_value = str(schedule.get("next_check_at") or "")
        try:
            next_text = datetime.fromisoformat(next_value).astimezone().strftime("%H:%M:%S")
        except ValueError:
            next_text = "—"
        self.time_label.setText(
            "最近 "
            + status.observed_at.astimezone().strftime("%H:%M:%S")
            + f" · 下次 {next_text}"
        )
        self.setStyleSheet(
            f"QFrame#heroBanner {{ background: {palette['surface']}; border: 1px solid {border}; "
            f"border-left: 4px solid {accent}; border-radius: 8px; }}"
        )


class ServiceRow(QFrame):
    def __init__(self, name: str, icon_name: str, *, last: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("serviceRowLast" if last else "serviceRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(11)
        self.icon_name = icon_name
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(28, 28)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self.name_label = QLabel(name)
        self.name_label.setObjectName("cardTitle")
        self.detail_label = QLabel("等待检测")
        self.detail_label.setObjectName("cardCaption")
        self.detail_label.setWordWrap(True)
        copy.addWidget(self.name_label)
        copy.addWidget(self.detail_label)
        layout.addLayout(copy, 1)
        self.meta_label = QLabel("—")
        self.meta_label.setObjectName("cardCaption")
        self.meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.meta_label)
        self.status_pill = PillLabel("等待数据", "neutral")
        self.status_pill.setMinimumWidth(76)
        layout.addWidget(self.status_pill)
        self.update_status("unknown", "等待检测")

    def update_status(self, raw: object, detail: str, meta: str = "") -> None:
        tone = sensor_tone(raw)
        color = {
            "success": LIGHT["green"],
            "warning": LIGHT["amber"],
            "danger": LIGHT["red"],
            "info": LIGHT["blue"],
            "neutral": LIGHT["text_muted"],
        }[tone]
        self.icon_label.setPixmap(icon_pixmap(self.icon_name, color, 19))
        self.status_pill.setText(sensor_label(raw))
        self.status_pill.set_tone(tone)
        self.detail_label.setText(_plain_reason(detail))
        self.meta_label.setText(meta or "")
        self.meta_label.setVisible(bool(meta))


class ComponentGroupCard(QFrame):
    def __init__(
        self,
        title: str,
        caption: str,
        icon_name: str,
        rows: tuple[tuple[str, str, str], ...],
        parent: QWidget | None = None,
        *,
        actions: tuple[QWidget, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("servicePanel")
        self.icon_name = icon_name
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("servicePanelHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(15, 13, 14, 10)
        header_layout.setSpacing(10)
        self.header_icon = QLabel()
        self.header_icon.setFixedSize(30, 30)
        self.header_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.header_icon)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("sectionTitle")
        caption_label = QLabel(caption)
        caption_label.setObjectName("sectionCaption")
        caption_label.setWordWrap(True)
        copy.addWidget(self.title_label)
        copy.addWidget(caption_label)
        header_layout.addLayout(copy, 1)
        self.state_pill = PillLabel("等待数据", "neutral")
        header_layout.addWidget(self.state_pill)
        for action in actions:
            header_layout.addWidget(action)
        layout.addWidget(header)
        self.rows: dict[str, ServiceRow] = {}
        for index, (component_id, label, row_icon) in enumerate(rows):
            row = ServiceRow(label, row_icon, last=index == len(rows) - 1)
            self.rows[component_id] = row
            layout.addWidget(row)
        self.update_component({"state": "unknown", "children": []})

    @staticmethod
    def _meta(child: dict[str, object]) -> str:
        metrics = child.get("metrics")
        values = metrics if isinstance(metrics, dict) else {}
        if child.get("id") == "qmt_api.process":
            processes = values.get("processes")
            return f"{len(processes)}个进程" if isinstance(processes, list) else ""
        latency = values.get("latency_ms")
        if child.get("id") in {"qmt_api.xtquant", "qmt_api.account"}:
            return f"{float(latency):.0f} ms" if isinstance(latency, (int, float)) else ""
        if child.get("id") == "qmt_api.trading_snapshot":
            if all(isinstance(values.get(key), int) for key in ("orders", "trades", "positions")):
                return (
                    f"委托 {values['orders']} · 成交 {values['trades']} · "
                    f"持仓 {values['positions']}"
                )
        if child.get("id") == "trade_system.data":
            products = values.get("products")
            errors = values.get("errors")
            if isinstance(products, int):
                return f"{products}项 · 异常 {errors or 0}"
        if child.get("id") == "trade_system.selection":
            engine = str(values.get("engine") or "")
            result = str(values.get("last_result") or "")
            return engine or (
                "最近成功"
                if result == "success"
                else "最近失败"
                if result == "failed"
                else ""
            )
        return ""

    def update_component(self, component: dict[str, object]) -> None:
        raw_state = str(component.get("state") or "unknown")
        tone = sensor_tone(raw_state)
        color = {
            "success": LIGHT["green"],
            "warning": LIGHT["amber"],
            "danger": LIGHT["red"],
            "info": LIGHT["blue"],
            "neutral": LIGHT["text_muted"],
        }[tone]
        self.header_icon.setPixmap(icon_pixmap(self.icon_name, color, 21))
        self.state_pill.setText(sensor_label(raw_state))
        self.state_pill.set_tone(tone)
        raw_children = component.get("children")
        children = raw_children if isinstance(raw_children, list) else []
        by_id = {
            str(child.get("id")): child
            for child in children
            if isinstance(child, dict)
        }
        for component_id, row in self.rows.items():
            child = by_id.get(component_id, {})
            row.update_status(
                child.get("state", "unknown"),
                str(child.get("reason") or "等待检测"),
                self._meta(child),
            )


class AttentionPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("safetyStrip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 12, 13, 12)
        layout.setSpacing(11)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(28, 28)
        layout.addWidget(self.icon_label)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self.title_label = QLabel("当前无需操作")
        self.title_label.setObjectName("cardTitle")
        self.message_label = QLabel("等待首次检测")
        self.message_label.setObjectName("cardCaption")
        self.message_label.setWordWrap(True)
        copy.addWidget(self.title_label)
        copy.addWidget(self.message_label)
        layout.addLayout(copy, 1)
        self.safety_pill = PillLabel("安全观察", "neutral")
        layout.addWidget(self.safety_pill)
        self.secondary_button = QToolButton()
        self.secondary_button.setObjectName("iconButton")
        self.secondary_button.setIcon(line_icon("refresh", 18))
        self.secondary_button.setToolTip("立即检测")
        self.secondary_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.secondary_button)
        self.action_button = QToolButton()
        self.action_button.setObjectName("primaryToolButton")
        self.action_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.action_button)

    def update_status(self, status: ServiceStatus) -> None:
        attention = status.attention or {}
        required = bool(attention.get("required"))
        level = str(attention.get("level") or "success")
        tone = "danger" if level == "critical" else "warning" if level == "warning" else "success"
        color = LIGHT["red"] if tone == "danger" else LIGHT["amber"] if tone == "warning" else LIGHT["green"]
        self.icon_label.setPixmap(icon_pixmap("warning" if required else "shield_check", color, 21))
        self.title_label.setText(str(attention.get("title") or "当前无需操作"))
        self.message_label.setText(str(attention.get("message") or "没有需要处理的异常"))
        self.safety_pill.setText("已授权恢复" if status.safety_live_actions else "安全观察")
        self.safety_pill.set_tone("success" if status.safety_live_actions else "neutral")
        target = str(attention.get("target") or "check")
        self.action_button.setText(str(attention.get("action") or "立即检测"))
        action_icon = "repair" if target == "restart" else "events" if target == "monitor" else "refresh"
        self.action_button.setIcon(QIcon(icon_pixmap(action_icon, "#FFFFFF", 18)))
        self.action_button.setProperty("target", target)


class EvidenceRow(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(8)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(18, 18)
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setObjectName("cardCaption")
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.text_label, 1)

    def update_value(self, text: str, tone: str = "neutral") -> None:
        colors = {
            "success": LIGHT["green"],
            "warning": LIGHT["amber"],
            "danger": LIGHT["red"],
            "info": LIGHT["blue"],
            "neutral": LIGHT["text_muted"],
        }
        icons = {"success": "check", "warning": "warning", "danger": "warning", "info": "info", "neutral": "info"}
        self.icon_label.setPixmap(icon_pixmap(icons[tone], colors[tone], 16))
        self.text_label.setText(text)


class StateDetailPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stateDetail")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        self.title_label = QLabel("当前判断")
        self.title_label.setObjectName("detailTitle")
        self.body_label = QLabel("等待监测证据")
        self.body_label.setObjectName("cardCaption")
        self.body_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        layout.addSpacing(3)
        self.evidence_rows = [EvidenceRow() for _ in range(3)]
        for row in self.evidence_rows:
            layout.addWidget(row)
        layout.addStretch()

    def update_status(self, status: ServiceStatus, *, verify_successes: int, verify_span: int) -> None:
        process = status.process or {}
        probe = status.probe or {}
        log = status.log or {}
        reconciliation = status.reconciliation or {}
        process_status = str(process.get("status", "unknown"))
        probe_status = str(probe.get("status", "unknown"))
        latency = probe.get("latency_ms")
        log_signal = str(log.get("signal", "neutral"))

        if status.action is RecommendedAction.WAIT_NETWORK:
            title = "等待网络恢复"
            body = "外部网络不可用时不会把连接失败误判为 QMT 故障，也不会触发重启。"
            items = [
                ("网络检查未通过，保持现场", "warning"),
                ("QMT 进程仍由监控器持续观察", sensor_tone(process_status)),
                ("网络恢复后自动重新执行只读探针", "info"),
            ]
        elif status.state is GuardianState.STARTING:
            title = "启动验证阶段"
            body = "Guardian 正在收集连续样本，启动宽限期内不会执行恢复动作。"
            items = [
                (f"QMT 进程：{sensor_label(process_status)}", sensor_tone(process_status)),
                (f"只读探针：{sensor_label(probe_status)}", sensor_tone(probe_status)),
                ("等待形成稳定健康基线", "info"),
            ]
        elif status.state is GuardianState.HEALTHY:
            title = "三层证据一致"
            body = "进程、只读业务探针与运行日志共同支持健康结论。"
            items = [
                (f"QMT 进程：{_plain_reason(process.get('reason'), sensor_label(process_status))}", sensor_tone(process_status)),
                (f"业务探针：{sensor_label(probe_status)} · {latency or 0:.0f} ms" if isinstance(latency, (int, float)) else f"业务探针：{sensor_label(probe_status)}", sensor_tone(probe_status)),
                (f"日志信号：{sensor_label(log_signal)}", sensor_tone(log_signal)),
            ]
        elif status.state is GuardianState.SUSPECT:
            title = "单次异常，继续复核"
            body = "短暂超时可能来自网络抖动或客户端忙碌。只有连续证据达到阈值后才会升级。"
            items = [
                (f"当前进程：{sensor_label(process_status)}", sensor_tone(process_status)),
                (f"当前探针：{sensor_label(probe_status)}", sensor_tone(probe_status)),
                ("本阶段不会立即重启 QMT", "success"),
            ]
        elif status.state is GuardianState.DEGRADED:
            title = "故障证据已确认"
            body = "监控状态机已跨过连续失败阈值；是否能执行恢复仍由安全授权单独决定。"
            items = [
                (f"QMT 进程：{sensor_label(process_status)}", sensor_tone(process_status)),
                (f"只读探针：{sensor_label(probe_status)}", sensor_tone(probe_status)),
                ("已保存脱敏证据，等待受控恢复或人工处理", "danger"),
            ]
        elif status.state is GuardianState.RECOVERING:
            title = "受控恢复步骤"
            body = "恢复没有虚构进度百分比；界面只展示已经进入的真实阶段。"
            items = [
                ("1. 已保存本次故障的脱敏证据", "success"),
                ("2. 正在优雅关闭并确认 QMT 进程状态", "info"),
                ("3. 将通过已配置的官方启动器重新启动", "neutral"),
            ]
        elif status.state is GuardianState.VERIFYING:
            title = "稳定性验证"
            body = "QMT 启动并不等于恢复完成；Guardian 会等待多次连续探针成功。"
            items = [
                (f"验证目标：连续 {verify_successes} 次成功", "info"),
                (f"最短稳定跨度：{verify_span} 秒", "info"),
                ("验证完成前不会自动恢复 Rocket 策略", "success"),
            ]
        elif status.state is GuardianState.MANUAL_REQUIRED:
            title = "实盘一致性需要人工确认"
            body = _plain_reason(reconciliation.get("reason"), "请在券商端核对当日委托、成交和当前持仓。")
            items = [
                (f"委托 {reconciliation.get('orders', '未知')} · 成交 {reconciliation.get('trades', '未知')}", "info"),
                (f"可撤委托 {reconciliation.get('cancelable_orders', '未知')} · 持仓项 {reconciliation.get('positions', '未知')}", "info"),
                ("确认只解除 Guardian 安全状态，不会启动交易策略", "danger"),
            ]
        elif status.state is GuardianState.LOCKOUT:
            title = "重试保护已生效"
            body = "自动恢复次数达到限制。锁定可以阻止故障循环持续干扰实盘环境。"
            items = [
                ("不会继续重复关闭或启动 QMT", "success"),
                ("请先检查登录、网络和券商端状态", "warning"),
                ("解除锁定后先重新检测，不会直接重启", "info"),
            ]
        else:
            title = "监控继续，恢复暂停"
            body = "进程、探针和日志仍会持续检测并记录；暂停仅影响自动恢复动作。"
            items = [
                (f"QMT 进程：{sensor_label(process_status)}", sensor_tone(process_status)),
                (f"只读探针：{sensor_label(probe_status)}", sensor_tone(probe_status)),
                ("点击恢复后会先重新检测当前状态", "info"),
            ]

        self.title_label.setText(title)
        self.body_label.setText(body)
        for row, (text, tone) in zip(self.evidence_rows, items, strict=True):
            row.update_value(text, tone)


@dataclass(frozen=True, slots=True)
class HealthSample:
    at: datetime
    state: GuardianState
    qmt_ok: bool | None
    trade_ok: bool | None
    latency_ms: float | None
    data_state: str = "unknown"
    selection_state: str = "unknown"
    order_state: str = "unknown"


def sample_from_status(status: ServiceStatus) -> HealthSample:
    components = status.components or {}
    qmt = components.get("qmt_api") if isinstance(components, dict) else {}
    trade = components.get("trade_system") if isinstance(components, dict) else {}
    qmt_value = qmt if isinstance(qmt, dict) else {}
    trade_value = trade if isinstance(trade, dict) else {}
    trade_children = trade_value.get("children")
    children = {
        str(child.get("id")): child
        for child in (trade_children if isinstance(trade_children, list) else [])
        if isinstance(child, dict)
    }
    qmt_state = str(qmt_value.get("state") or (status.process or {}).get("status", "unknown"))
    trade_state = str(trade_value.get("state") or "unknown")
    latency = (status.probe or {}).get("latency_ms")
    return HealthSample(
        at=status.observed_at.astimezone(),
        state=status.state,
        qmt_ok=(
            True
            if qmt_state == "healthy"
            else None
            if qmt_state in {"unknown", "recovering"}
            else False
        ),
        trade_ok=(
            True
            if trade_state in {"healthy", "idle"}
            else None
            if trade_state == "unknown"
            else False
        ),
        latency_ms=float(latency) if isinstance(latency, (int, float)) and latency >= 0 else None,
        data_state=str(children.get("trade_system.data", {}).get("state") or "unknown"),
        selection_state=str(
            (
                children.get("trade_system.selection")
                or children.get("trade_system.backtest")
                or {}
            ).get("state")
            or "unknown"
        ),
        order_state=str(children.get("trade_system.order", {}).get("state") or "unknown"),
    )


def sample_from_document(document: dict[str, object]) -> HealthSample | None:
    try:
        at = datetime.fromisoformat(str(document.get("observed_at"))).astimezone()
        state = GuardianState(str(document.get("state")))
    except (TypeError, ValueError):
        return None
    components = document.get("components")
    component_values = components if isinstance(components, dict) else {}
    qmt = component_values.get("qmt_api")
    trade = component_values.get("trade_system")
    qmt_value = qmt if isinstance(qmt, dict) else {}
    trade_value = trade if isinstance(trade, dict) else {}
    children_raw = trade_value.get("children")
    children = {
        str(child.get("id")): child
        for child in (children_raw if isinstance(children_raw, list) else [])
        if isinstance(child, dict)
    }
    probe = document.get("probe")
    probe_value = probe if isinstance(probe, dict) else {}
    latency = probe_value.get("latency_ms")
    qmt_state = str(qmt_value.get("state") or "unknown")
    trade_state = str(trade_value.get("state") or "unknown")
    return HealthSample(
        at=at,
        state=state,
        qmt_ok=True if qmt_state == "healthy" else None if qmt_state in {"unknown", "recovering"} else False,
        trade_ok=True if trade_state in {"healthy", "idle"} else None if trade_state == "unknown" else False,
        latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
        data_state=str(children.get("trade_system.data", {}).get("state") or "unknown"),
        selection_state=str(
            (
                children.get("trade_system.selection")
                or children.get("trade_system.backtest")
                or {}
            ).get("state")
            or "unknown"
        ),
        order_state=str(children.get("trade_system.order", {}).get("state") or "unknown"),
    )


def filter_samples(samples: Iterable[HealthSample], range_key: str) -> list[HealthSample]:
    values = list(samples)
    if not values:
        return []
    end = max(item.at for item in values)
    if range_key == "1h":
        start = end - timedelta(hours=1)
    elif range_key == "today":
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = end - timedelta(days=7)
    return [item for item in values if item.at >= start]


class HealthTimelineWidget(QWidget):
    def __init__(self, *, compact: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.samples: list[HealthSample] = []
        self.operation_markers: list[dict[str, Any]] = []
        self.range_key = "1h"
        self.dark = False
        self.compact = compact
        self.setMinimumHeight(108 if compact else 164)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_samples(self, samples: Iterable[HealthSample]) -> None:
        self.samples = list(samples)
        self.update()

    def set_operation_markers(
        self, markers: Iterable[dict[str, Any]]
    ) -> None:
        self.operation_markers = list(markers)
        self.update()

    def set_range(self, range_key: str) -> None:
        self.range_key = range_key
        self.update()

    def set_dark(self, dark: bool) -> None:
        self.dark = dark
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = DARK if self.dark else LIGHT
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["surface"]))
        painter.drawRoundedRect(QRectF(rect), 9, 9)
        painter.setPen(QPen(QColor(c["border"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(rect), 9, 9)

        values = filter_samples(self.samples, self.range_key)
        title_font = QFont(self.font())
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(c["text"]))
        painter.drawText(QRectF(15, 10, rect.width() - 30, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "QMT API 与 Trade System 健康时间轴")
        if not values:
            painter.setFont(self.font())
            painter.setPen(QColor(c["text_muted"]))
            painter.drawText(QRectF(15, 38, rect.width() - 30, rect.height() - 46), Qt.AlignmentFlag.AlignCenter, "等待当前会话的监测数据")
            return

        left = 100 if not self.compact else 84
        right = 15
        top = 39
        bottom = 25 if not self.compact else 17
        lane_gap = 10 if not self.compact else 7
        lane_height = max(8, (rect.height() - top - bottom - lane_gap) / 2)
        plot_left = left
        plot_width = max(20.0, rect.width() - left - right)
        start = values[0].at
        end = values[-1].at
        span = max(1.0, (end - start).total_seconds())

        lanes = (
            ("QMT API", lambda s: s.qmt_ok),
            ("Trade System", lambda s: s.trade_ok),
        )
        painter.setFont(self.font())
        for lane_index, (label, getter) in enumerate(lanes):
            y = top + lane_index * (lane_height + lane_gap)
            painter.setPen(QColor(c["text_muted"]))
            painter.drawText(QRectF(12, y, left - 19, lane_height), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c["surface_alt"]))
            painter.drawRoundedRect(QRectF(plot_left, y + 2, plot_width, lane_height - 4), 3, 3)
            span_start = 0
            for index in range(1, len(values) + 1):
                if index < len(values) and getter(values[index]) == getter(values[span_start]):
                    continue
                x0 = plot_left + ((values[span_start].at - start).total_seconds() / span) * plot_width
                x1 = (
                    plot_left + ((values[index].at - start).total_seconds() / span) * plot_width
                    if index < len(values)
                    else plot_left + plot_width
                )
                current = getter(values[span_start])
                color = c["green"] if current is True else c["red"] if current is False else c["border_strong"]
                painter.setBrush(QColor(color))
                painter.drawRoundedRect(QRectF(x0, y + 2, max(3.0, x1 - x0 + 0.5), lane_height - 4), 2, 2)
                span_start = index

        marker_bottom = top + 2 * lane_height + lane_gap
        visible_markers: list[tuple[datetime, dict[str, Any]]] = []
        for marker in self.operation_markers:
            try:
                marker_at = datetime.fromisoformat(str(marker.get("started_at") or ""))
            except ValueError:
                continue
            if marker_at.tzinfo is None:
                marker_at = marker_at.replace(tzinfo=start.tzinfo)
            if start <= marker_at <= end:
                visible_markers.append((marker_at, marker))
        for marker_at, marker in visible_markers:
            x = plot_left + ((marker_at - start).total_seconds() / span) * plot_width
            status = str(marker.get("status") or "")
            marker_color = (
                c["green"]
                if status == "succeeded"
                else c["red"]
                if status == "failed"
                else c["amber"]
                if status in {"blocked", "verifying", "in_progress"}
                else c["indigo"]
            )
            pen = QPen(QColor(marker_color), 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, top - 2), QPointF(x, marker_bottom))
            painter.setPen(QPen(QColor(c["surface"]), 1))
            painter.setBrush(QColor(marker_color))
            painter.drawEllipse(QPointF(x, top - 5), 4, 4)

        if visible_markers and not self.compact:
            painter.setFont(self.font())
            painter.setPen(QColor(c["text_muted"]))
            painter.drawText(
                QRectF(rect.width() - 106, 10, 90, 20),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"● 操作点 {len(visible_markers)}",
            )

        if not self.compact:
            painter.setPen(QColor(c["text_faint"]))
            time_y = rect.height() - 19
            painter.drawText(QRectF(plot_left, time_y, 90, 14), Qt.AlignmentFlag.AlignLeft, start.strftime("%H:%M"))
            painter.drawText(QRectF(plot_left + plot_width - 90, time_y, 90, 14), Qt.AlignmentFlag.AlignRight, end.strftime("%H:%M"))


class LatencyChartWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.samples: list[HealthSample] = []
        self.range_key = "1h"
        self.dark = False
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_samples(self, samples: Iterable[HealthSample]) -> None:
        self.samples = list(samples)
        self.update()

    def set_range(self, range_key: str) -> None:
        self.range_key = range_key
        self.update()

    def set_dark(self, dark: bool) -> None:
        self.dark = dark
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = DARK if self.dark else LIGHT
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["surface"]))
        painter.drawRoundedRect(QRectF(rect), 9, 9)
        painter.setPen(QPen(QColor(c["border"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(rect), 9, 9)

        title_font = QFont(self.font())
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(c["text"]))
        painter.drawText(QRectF(15, 10, rect.width() - 30, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "QMT API 延迟")

        values = [sample for sample in filter_samples(self.samples, self.range_key) if sample.latency_ms is not None]
        if not values:
            painter.setFont(self.font())
            painter.setPen(QColor(c["text_muted"]))
            painter.drawText(QRectF(15, 40, rect.width() - 30, rect.height() - 50), Qt.AlignmentFlag.AlignCenter, "等待 QMT API 延迟样本")
            return

        plot = QRectF(56, 45, rect.width() - 75, rect.height() - 77)
        max_value = max(50.0, max(float(sample.latency_ms or 0) for sample in values) * 1.2)
        start = values[0].at
        end = values[-1].at
        span = max(1.0, (end - start).total_seconds())

        painter.setFont(self.font())
        for index in range(4):
            y = plot.top() + (plot.height() / 3) * index
            value = max_value * (1 - index / 3)
            painter.setPen(QPen(QColor(c["border"]), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(c["text_faint"]))
            painter.drawText(QRectF(4, y - 8, 45, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{value:.0f}")

        def point(sample: HealthSample) -> QPointF:
            x = plot.left() + ((sample.at - start).total_seconds() / span) * plot.width()
            y = plot.bottom() - (float(sample.latency_ms or 0) / max_value) * plot.height()
            return QPointF(x, y)

        if len(values) == 1:
            only = point(values[0])
            path = QPainterPath(QPointF(plot.left(), only.y()))
            path.lineTo(QPointF(plot.right(), only.y()))
        else:
            path = QPainterPath(point(values[0]))
            for sample in values[1:]:
                path.lineTo(point(sample))
        fill_path = QPainterPath(path)
        fill_path.lineTo(plot.right(), plot.bottom())
        fill_path.lineTo(plot.left(), plot.bottom())
        fill_path.closeSubpath()
        gradient = QLinearGradient(0, plot.top(), 0, plot.bottom())
        top_color = QColor(c["indigo"])
        top_color.setAlpha(70)
        bottom_color = QColor(c["indigo"])
        bottom_color.setAlpha(3)
        gradient.setColorAt(0, top_color)
        gradient.setColorAt(1, bottom_color)
        painter.fillPath(fill_path, gradient)
        painter.setPen(QPen(QColor(c["indigo"]), 2.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(path)
        last = (
            QPointF(plot.right(), point(values[-1]).y())
            if len(values) == 1
            else point(values[-1])
        )
        painter.setPen(QPen(QColor(c["surface"]), 2))
        painter.setBrush(QColor(c["indigo"]))
        painter.drawEllipse(last, 4.5, 4.5)
        painter.setPen(QColor(c["text_faint"]))
        painter.drawText(QRectF(plot.left(), plot.bottom() + 8, 90, 16), Qt.AlignmentFlag.AlignLeft, start.strftime("%H:%M"))
        painter.drawText(QRectF(plot.right() - 90, plot.bottom() + 8, 90, 16), Qt.AlignmentFlag.AlignRight, end.strftime("%H:%M"))


class TaskOutcomeChartWidget(QWidget):
    """Compact state ribbons for Fuel, Aqua/Zeus selection, and Rocket."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.samples: list[HealthSample] = []
        self.range_key = "1h"
        self.dark = False
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_samples(self, samples: Iterable[HealthSample]) -> None:
        self.samples = list(samples)
        self.update()

    def set_range(self, range_key: str) -> None:
        self.range_key = range_key
        self.update()

    def set_dark(self, dark: bool) -> None:
        self.dark = dark
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = DARK if self.dark else LIGHT
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["surface"]))
        painter.drawRoundedRect(QRectF(rect), 9, 9)
        painter.setPen(QPen(QColor(c["border"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(rect), 9, 9)

        title_font = QFont(self.font())
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(c["text"]))
        painter.drawText(
            QRectF(15, 10, rect.width() - 30, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "Trade System 任务结果与新鲜度",
        )
        values = filter_samples(self.samples, self.range_key)
        if not values:
            painter.setFont(self.font())
            painter.setPen(QColor(c["text_muted"]))
            painter.drawText(
                QRectF(15, 40, rect.width() - 30, rect.height() - 50),
                Qt.AlignmentFlag.AlignCenter,
                "等待 Fuel、选股内核与 Rocket 样本",
            )
            return

        lanes = (
            ("数据 · Fuel", lambda sample: sample.data_state),
            ("选股 · Aqua / Zeus", lambda sample: sample.selection_state),
            ("下单 · Rocket", lambda sample: sample.order_state),
        )
        left, right, top, bottom, gap = 150, 15, 45, 27, 10
        lane_height = max(10.0, (rect.height() - top - bottom - gap * 2) / 3)
        plot_width = max(20.0, rect.width() - left - right)
        start, end = values[0].at, values[-1].at
        span = max(1.0, (end - start).total_seconds())
        colors = {
            "healthy": c["green"],
            "idle": c["idle"],
            "warning": c["amber"],
            "critical": c["red"],
            "recovering": c["indigo"],
            "unknown": c["border_strong"],
        }
        painter.setFont(self.font())
        for lane_index, (label, getter) in enumerate(lanes):
            y = top + lane_index * (lane_height + gap)
            painter.setPen(QColor(c["text_muted"]))
            painter.drawText(
                QRectF(10, y, left - 20, lane_height),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c["surface_alt"]))
            painter.drawRoundedRect(QRectF(left, y + 2, plot_width, lane_height - 4), 3, 3)
            span_start = 0
            for index in range(1, len(values) + 1):
                if index < len(values) and getter(values[index]) == getter(values[span_start]):
                    continue
                x0 = left + ((values[span_start].at - start).total_seconds() / span) * plot_width
                x1 = (
                    left + ((values[index].at - start).total_seconds() / span) * plot_width
                    if index < len(values)
                    else left + plot_width
                )
                painter.setBrush(QColor(colors.get(str(getter(values[span_start])), c["border_strong"])))
                painter.drawRoundedRect(
                    QRectF(x0, y + 2, max(3.0, x1 - x0 + 0.5), lane_height - 4),
                    2,
                    2,
                )
                span_start = index
        painter.setPen(QColor(c["text_faint"]))
        painter.drawText(QRectF(left, rect.height() - 19, 90, 14), Qt.AlignmentFlag.AlignLeft, start.strftime("%H:%M"))
        painter.drawText(QRectF(left + plot_width - 90, rect.height() - 19, 90, 14), Qt.AlignmentFlag.AlignRight, end.strftime("%H:%M"))


def compute_trend_metrics(samples: Iterable[HealthSample], range_key: str) -> tuple[str, str, str, str, str, str]:
    values = filter_samples(samples, range_key)
    if not values:
        return "—", "—", "—", "—", "—", "等待数据"
    qmt_values = [sample.qmt_ok for sample in values if sample.qmt_ok is not None]
    availability = (
        sum(1 for value in qmt_values if value) / len(qmt_values) * 100
        if qmt_values
        else None
    )
    latencies = [sample.latency_ms for sample in values if sample.latency_ms is not None]
    average_latency = sum(latencies) / len(latencies) if latencies else None
    ordered_latencies = sorted(float(value) for value in latencies)
    p95_latency = (
        ordered_latencies[
            min(
                len(ordered_latencies) - 1,
                max(0, math.ceil(len(ordered_latencies) * 0.95) - 1),
            )
        ]
        if ordered_latencies
        else None
    )
    trade_values = [sample.trade_ok for sample in values if sample.trade_ok is not None]
    trade_success = (
        sum(1 for value in trade_values if value) / len(trade_values) * 100
        if trade_values
        else None
    )
    incidents = 0
    previous_ok = True
    for sample in values:
        current_ok = (
            sample.state in {GuardianState.HEALTHY, GuardianState.STARTING, GuardianState.VERIFYING}
            and sample.trade_ok is not False
        )
        if previous_ok and not current_ok:
            incidents += 1
        previous_ok = current_ok
    span = values[-1].at - values[0].at
    if span.total_seconds() < 60:
        coverage = "已持久化 · 少于 1 分钟"
    elif span.total_seconds() < 3600:
        coverage = f"已持久化 · {span.total_seconds() / 60:.0f} 分钟"
    else:
        coverage = f"已持久化 · {span.total_seconds() / 3600:.1f} 小时"
    return (
        f"{availability:.1f}%" if availability is not None else "\u2014",
        f"{average_latency:.0f} ms" if average_latency is not None else "—",
        f"{p95_latency:.0f} ms" if p95_latency is not None else "—",
        f"{trade_success:.1f}%" if trade_success is not None else "—",
        str(incidents),
        coverage,
    )
