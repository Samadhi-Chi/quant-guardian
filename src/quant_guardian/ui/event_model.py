from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from quant_guardian.ui.design_system import LIGHT


class EventTableModel(QAbstractTableModel):
    COLUMNS = ("时间", "组件", "类型", "级别", "摘要")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.events: list[dict[str, Any]] = []

    def set_events(self, events: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.events = list(events)
        self.endResetModel()

    def append_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        first = len(self.events)
        self.beginInsertRows(QModelIndex(), first, first + len(events) - 1)
        self.events.extend(events)
        self.endInsertRows()

    def event_at(self, row: int) -> dict[str, Any] | None:
        return self.events[row] if 0 <= row < len(self.events) else None

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.events)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.COLUMNS)
        ):
            return self.COLUMNS[section]
        return None

    @staticmethod
    def _severity_text(value: object) -> str:
        return {
            "info": "信息",
            "warning": "警告",
            "critical": "严重",
        }.get(str(value), str(value or ""))

    @staticmethod
    def _component_text(value: object) -> str:
        raw = str(value or "")
        if raw.startswith("qmt_api"):
            return "QMT API"
        if raw.startswith("trade_system"):
            return "Trade System"
        if raw == "quant_guardian":
            return "Guardian"
        return raw or "系统"

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.events)):
            return None
        event = self.events[index.row()]
        column = index.column()
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            time_value = str(event.get("time") or "").replace("T", " ")[:19]
            values = (
                time_value,
                self._component_text(event.get("component_id")),
                str(event.get("event_type") or ""),
                self._severity_text(event.get("severity")),
                str(event.get("summary") or ""),
            )
            return values[column]
        if role == Qt.ItemDataRole.ForegroundRole and column == 3:
            severity = str(event.get("severity") or "")
            return QColor(
                LIGHT["red"]
                if severity == "critical"
                else LIGHT["amber"]
                if severity == "warning"
                else LIGHT["text_muted"]
            )
        if role == Qt.ItemDataRole.UserRole:
            return event
        return None


class OperationTableModel(QAbstractTableModel):
    COLUMNS = ("开始时间", "操作", "发起方", "结果", "耗时", "摘要")

    OPERATION_NAMES = {
        "qmt_restart": "重启 QMT",
        "quantclass_restart": "重启 QuantClass",
        "manual_check": "立即检测",
        "guardian_worker_restart": "恢复监控线程",
        "recovery_control": "恢复控制",
        "settings_change": "保存设置",
        "diagnostic_export": "导出诊断",
        "remote_command": "远程命令",
    }
    INITIATOR_NAMES = {
        "automatic": "自动恢复",
        "manual": "人工",
        "watchdog": "看门狗",
        "remote_telegram": "Telegram 远程",
        "remote_weixin": "个人微信远程",
    }
    STATUS_NAMES = {
        "succeeded": "验证成功",
        "failed": "失败",
        "blocked": "已阻断",
        "verifying": "验证中",
        "in_progress": "执行中",
        "cancelled": "已取消",
        "awaiting_confirmation": "待确认",
        "sent": "已发送",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.operations: list[dict[str, Any]] = []

    def set_operations(self, operations: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.operations = list(operations)
        self.endResetModel()

    def append_operations(self, operations: list[dict[str, Any]]) -> None:
        if not operations:
            return
        first = len(self.operations)
        self.beginInsertRows(QModelIndex(), first, first + len(operations) - 1)
        self.operations.extend(operations)
        self.endInsertRows()

    def operation_at(self, row: int) -> dict[str, Any] | None:
        return self.operations[row] if 0 <= row < len(self.operations) else None

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.operations)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.COLUMNS)
        ):
            return self.COLUMNS[section]
        return None

    @staticmethod
    def _duration_text(value: object) -> str:
        if not isinstance(value, (int, float)):
            return "—"
        seconds = float(value) / 1000
        if seconds < 1:
            return f"{int(value)} ms"
        if seconds < 60:
            return f"{seconds:.1f} 秒"
        return f"{seconds / 60:.1f} 分"

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.operations)):
            return None
        operation = self.operations[index.row()]
        column = index.column()
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            started_at = str(operation.get("started_at") or "").replace("T", " ")[:19]
            if len(started_at) >= 19:
                started_at = started_at[5:19]
            operation_type = str(operation.get("operation_type") or "")
            initiator = str(operation.get("initiator") or "")
            status = str(operation.get("status") or "")
            values = (
                started_at,
                self.OPERATION_NAMES.get(operation_type, operation_type),
                self.INITIATOR_NAMES.get(initiator, initiator),
                self.STATUS_NAMES.get(status, status),
                self._duration_text(operation.get("duration_ms")),
                str(operation.get("summary") or ""),
            )
            return values[column]
        if role == Qt.ItemDataRole.ForegroundRole and column == 3:
            status = str(operation.get("status") or "")
            return QColor(
                LIGHT["green"]
                if status == "succeeded"
                else LIGHT["red"]
                if status == "failed"
                else LIGHT["amber"]
                if status in {"blocked", "verifying", "in_progress"}
                else LIGHT["text_muted"]
            )
        if role == Qt.ItemDataRole.UserRole:
            return operation
        return None


class GatewayActivityTableModel(QAbstractTableModel):
    COLUMNS = ("时间", "通道", "类型", "动作", "结果", "说明")

    CHANNEL_NAMES = {"telegram": "Telegram", "weixin": "个人微信"}
    KIND_NAMES = {"command": "远程命令", "delivery": "消息播报"}
    ACTION_NAMES = {
        "status": "查询状态",
        "check": "立即检测",
        "incidents": "查询故障",
        "operations": "查询操作",
        "restart_qmt": "重启 QMT",
        "pair": "绑定私聊",
        "message": "发送消息",
    }
    STATUS_NAMES = {
        "sent": "已发送",
        "succeeded": "成功",
        "accepted": "已受理",
        "failed": "失败",
        "blocked": "已阻断",
        "cancelled": "已取消",
        "pending": "待发送",
        "delivering": "发送中",
        "awaiting_confirmation": "待确认",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[dict[str, Any]] = []

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.COLUMNS)
        ):
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        row = self.rows[index.row()]
        status = str(row.get("status") or "")
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            time_value = str(row.get("time") or "").replace("T", " ")[:19]
            if len(time_value) >= 19:
                time_value = time_value[5:19]
            channel = str(row.get("channel") or "")
            kind = str(row.get("kind") or "")
            action = str(row.get("action") or "")
            values = (
                time_value,
                self.CHANNEL_NAMES.get(channel, channel),
                self.KIND_NAMES.get(kind, kind),
                self.ACTION_NAMES.get(action, action),
                self.STATUS_NAMES.get(status, status),
                str(row.get("reason") or ""),
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 4:
            return QColor(
                LIGHT["green"]
                if status in {"sent", "succeeded"}
                else LIGHT["red"]
                if status == "failed"
                else LIGHT["amber"]
                if status in {"blocked", "pending", "delivering", "awaiting_confirmation"}
                else LIGHT["text_muted"]
            )
        if role == Qt.ItemDataRole.UserRole:
            return row
        return None
