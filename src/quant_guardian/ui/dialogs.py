from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from quant_guardian.config import AppConfig, save_config
from quant_guardian.service import ServiceStatus
from quant_guardian.ui.design_system import LIGHT, icon_pixmap
from quant_guardian.ui.widgets import PillLabel


def _dialog_header(icon_name: str, title: str, subtitle: str) -> tuple[QWidget, QLabel]:
    header = QWidget()
    layout = QHBoxLayout(header)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    icon = QLabel()
    icon.setFixedSize(40, 40)
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setPixmap(icon_pixmap(icon_name, LIGHT["indigo"], 24))
    icon.setStyleSheet(f"background: {LIGHT['indigo_soft']}; border-radius: 10px;")
    layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
    copy = QVBoxLayout()
    copy.setSpacing(3)
    title_label = QLabel(title)
    title_label.setObjectName("dialogTitle")
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("cardCaption")
    subtitle_label.setWordWrap(True)
    copy.addWidget(title_label)
    copy.addWidget(subtitle_label)
    layout.addLayout(copy, 1)
    return header, title_label


class ManualConfirmDialog(QDialog):
    def __init__(self, status: ServiceStatus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认实盘一致性")
        self.setMinimumWidth(570)
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 20)
        root.setSpacing(14)
        header, _ = _dialog_header(
            "hand",
            "确认恢复后的实盘状态",
            "QMT 已恢复，但 Quant Guardian 不会替你启动或恢复 Quantclass / Rocket 策略。",
        )
        root.addWidget(header)

        risk = QFrame()
        risk.setObjectName("stateDetail")
        risk_layout = QVBoxLayout(risk)
        risk_layout.setContentsMargins(14, 12, 14, 12)
        risk_title = QLabel("重复下单风险")
        risk_title.setObjectName("cardTitle")
        risk_title.setStyleSheet(f"color: {LIGHT['red']};")
        risk_copy = QLabel("请先在券商端逐项核对当日委托、成交和当前持仓，再决定是否恢复策略。")
        risk_copy.setObjectName("cardCaption")
        risk_copy.setWordWrap(True)
        risk_layout.addWidget(risk_title)
        risk_layout.addWidget(risk_copy)
        root.addWidget(risk)

        reconciliation = status.reconciliation or {}
        summary = QPlainTextEdit()
        summary.setReadOnly(True)
        summary.setMaximumHeight(132)
        summary.setPlainText(
            "\n".join(
                [
                    f"核对状态：{reconciliation.get('reason', '需要人工核对')}",
                    f"当日委托：{reconciliation.get('orders', '未知')}",
                    f"可撤委托：{reconciliation.get('cancelable_orders', '未知')}",
                    f"当日成交：{reconciliation.get('trades', '未知')}",
                    f"当前持仓项：{reconciliation.get('positions', '未知')}",
                ]
            )
        )
        root.addWidget(summary)

        self.acknowledge = QCheckBox("我已核对当日委托、成交和当前持仓，并理解重复下单风险。")
        self.acknowledge.setObjectName("manualAcknowledge")
        root.addWidget(self.acknowledge)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("继续保持暂停")
        cancel.clicked.connect(self.reject)
        self.ok_button = QPushButton("记录为已确认")
        self.ok_button.setProperty("variant", "danger")
        self.ok_button.setEnabled(False)
        self.ok_button.clicked.connect(self.accept)
        self.acknowledge.toggled.connect(self.ok_button.setEnabled)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok_button)
        root.addLayout(buttons)


class RestartConfirmDialog(QDialog):
    def __init__(self, safety_reason: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("受控重启 QMT")
        self.setMinimumWidth(550)
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 20)
        root.setSpacing(14)
        header, _ = _dialog_header(
            "repair",
            "确认重启 QMT？",
            "这是你主动发起的人工操作；确认后直接执行，不受观察模式或自动恢复授权限制。",
        )
        root.addWidget(header)

        steps = QFrame()
        steps.setObjectName("stateDetail")
        steps_layout = QVBoxLayout(steps)
        steps_layout.setContentsMargins(14, 12, 14, 12)
        for index, text in enumerate(
            (
                "保存本次操作前的脱敏诊断证据",
                "优雅关闭 QMT，并在超时后停止残留进程",
                "使用配置中的官方启动器重新启动 QMT",
                "连续执行只读探针验证，不自动恢复策略",
            ),
            start=1,
        ):
            row = QLabel(f"{index}.  {text}")
            row.setObjectName("cardCaption")
            steps_layout.addWidget(row)
        root.addWidget(steps)

        safety = QLabel(
            "自动恢复状态："
            + safety_reason
            + "\n该状态只约束自动恢复，不会阻止本次已确认的人工重启。"
        )
        safety.setObjectName("cardCaption")
        safety.setWordWrap(True)
        root.addWidget(safety)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("确认重启 QMT")
        confirm.setProperty("variant", "danger")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        root.addLayout(buttons)


class QuantclassRestartConfirmDialog(QDialog):
    def __init__(
        self,
        executable: str,
        *,
        rocket_active: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("重启 Quantclass")
        self.setMinimumWidth(560)
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 20)
        root.setSpacing(14)
        header, _ = _dialog_header(
            "repair",
            "确认重启 Quantclass？",
            "这是你主动发起的人工操作；确认后只重启Quantclass客户端，不受观察模式限制。",
        )
        root.addWidget(header)

        warning = QFrame()
        warning.setObjectName("stateDetail")
        warning_layout = QVBoxLayout(warning)
        warning_layout.setContentsMargins(14, 12, 14, 12)
        warning_title = QLabel("运行影响")
        warning_title.setObjectName("cardTitle")
        warning_title.setStyleSheet(f"color: {LIGHT['amber']};")
        warning_copy = QLabel(
            "Rocket当前处于活动状态。重启客户端可能影响实盘操作，请确认已完成必要核对。"
            if rocket_active
            else "客户端重启期间，Quantclass界面和任务调度可能短暂不可用；Fuel、Zeus与Rocket进程不会被Quant Guardian主动终止。"
        )
        warning_copy.setObjectName("cardCaption")
        warning_copy.setWordWrap(True)
        warning_layout.addWidget(warning_title)
        warning_layout.addWidget(warning_copy)
        root.addWidget(warning)

        target = QLabel("客户端：" + executable)
        target.setObjectName("cardCaption")
        target.setWordWrap(True)
        root.addWidget(target)

        mode = QLabel(
            "系统仍会执行精确进程路径校验、并发锁和启动结果验证；这些保护不会因人工确认而关闭。"
        )
        mode.setObjectName("cardCaption")
        mode.setWordWrap(True)
        root.addWidget(mode)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("确认重启 Quantclass")
        confirm.setProperty("variant", "danger")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        root.addLayout(buttons)


class FirstRunDialog(QDialog):
    """Three-step first-run flow. It never creates the recovery sentinel."""

    def __init__(self, config: AppConfig, config_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.config_path = config_path
        self.setWindowTitle("欢迎使用 Quant Guardian")
        self.setMinimumSize(650, 520)
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)

        brand = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(icon_pixmap("shield_check", LIGHT["indigo"], 27))
        icon.setFixedSize(34, 34)
        title = QLabel("Quant Guardian")
        title.setObjectName("dialogTitle")
        brand.addWidget(icon)
        brand.addWidget(title)
        brand.addStretch()
        self.step_pill = PillLabel("第 1 步 / 共 3 步", "info")
        brand.addWidget(self.step_pill)
        root.addLayout(brand)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome_page())
        self.stack.addWidget(self._build_paths_page())
        self.stack.addWidget(self._build_finish_page())
        root.addWidget(self.stack, 1)

        divider = QFrame()
        divider.setObjectName("divider")
        root.addWidget(divider)
        nav = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.back_button.clicked.connect(self.go_back)
        self.skip_button = QPushButton("稍后配置")
        self.skip_button.setProperty("variant", "ghost")
        self.skip_button.clicked.connect(self.reject)
        self.next_button = QPushButton("下一步")
        self.next_button.setProperty("variant", "primary")
        self.next_button.clicked.connect(self.go_next)
        nav.addWidget(self.skip_button)
        nav.addStretch()
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)
        root.addLayout(nav)
        self._sync_step()

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(14)
        heading = QLabel("让实盘监控更安静，也更可解释")
        heading.setObjectName("pageTitle")
        caption = QLabel("Guardian 通过进程、只读业务探针和日志三层证据判断 QMT 健康状态。")
        caption.setObjectName("pageSubtitle")
        caption.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(caption)
        for icon_name, title, body in (
            ("activity", "避免误重启", "单次超时只进入复核状态，连续证据达到阈值后才确认故障。"),
            ("shield_check", "默认安全观察", "首次启动只监控和告警，界面不能绕过恢复授权。"),
            ("hand", "策略恢复由你决定", "QMT 恢复后必须核对实盘，Guardian 不会代替你恢复策略。"),
        ):
            card = QFrame()
            card.setObjectName("card")
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 12, 14, 12)
            icon = QLabel()
            icon.setPixmap(icon_pixmap(icon_name, LIGHT["indigo"], 21))
            icon.setFixedSize(28, 28)
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
            copy = QVBoxLayout()
            copy.setSpacing(2)
            name = QLabel(title)
            name.setObjectName("cardTitle")
            description = QLabel(body)
            description.setObjectName("cardCaption")
            description.setWordWrap(True)
            copy.addWidget(name)
            copy.addWidget(description)
            row.addLayout(copy, 1)
            layout.addWidget(card)
        layout.addStretch()
        return page

    def _build_paths_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        heading = QLabel("确认本机组件路径")
        heading.setObjectName("pageTitle")
        caption = QLabel("这些路径只用于本机健康检查和官方启动器调用，不会上传。")
        caption.setObjectName("pageSubtitle")
        layout.addWidget(heading)
        layout.addWidget(caption)
        form_card = QFrame()
        form_card.setObjectName("formSection")
        form = QFormLayout(form_card)
        form.setContentsMargins(16, 14, 16, 14)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(11)
        self.launcher_edit = QLineEdit(self.config.qmt.launcher)
        self.userdata_edit = QLineEdit(self.config.qmt.userdata_directory)
        self.log_edit = QLineEdit(self.config.qmt.log_directory)
        self.probe_python_edit = QLineEdit(self.config.probe.python_executable)
        self.xtquant_edit = QLineEdit(self.config.probe.xtquant_parent)
        self.rocket_log_edit = QLineEdit(self.config.rocket.log_directory)
        form.addRow("QMT 官方启动器", self.launcher_edit)
        form.addRow("QMT 用户数据目录", self.userdata_edit)
        form.addRow("QMT 日志目录", self.log_edit)
        form.addRow("只读探针 Python", self.probe_python_edit)
        form.addRow("XtQuant 父目录", self.xtquant_edit)
        form.addRow("Rocket 日志目录", self.rocket_log_edit)
        layout.addWidget(form_card)
        layout.addStretch()
        return page

    def _build_finish_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(14)
        heading = QLabel("从安全观察模式开始")
        heading.setObjectName("pageTitle")
        caption = QLabel("建议先完成至少一个完整交易日的观察，核对误报、漏报和探针稳定性。")
        caption.setObjectName("pageSubtitle")
        caption.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(caption)
        card = QFrame()
        card.setObjectName("stateDetail")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(9)
        mode = QHBoxLayout()
        mode.addWidget(QLabel("运行模式"))
        mode.addStretch()
        mode.addWidget(PillLabel("安全观察", "success"))
        card_layout.addLayout(mode)
        for text in (
            "监控 QMT 进程、业务探针、日志与 Rocket 活跃状态",
            "记录状态变化并发送桌面通知",
            "不会关闭或启动 QMT，也不会操作策略",
            "恢复模式必须通过独立脚本创建安全授权哨兵",
        ):
            line = QLabel("✓  " + text)
            line.setObjectName("cardCaption")
            card_layout.addWidget(line)
        layout.addWidget(card)
        note = QLabel("完成后可随时在“设置”中调整阈值、交易时段、通知和数据保留策略。")
        note.setObjectName("cardCaption")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _sync_step(self) -> None:
        index = self.stack.currentIndex()
        self.step_pill.setText(f"第 {index + 1} 步 / 共 3 步")
        self.back_button.setVisible(index > 0)
        self.skip_button.setVisible(index < 2)
        self.next_button.setText("完成并开始监控" if index == 2 else "下一步")

    def go_back(self) -> None:
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))
        self._sync_step()

    def go_next(self) -> None:
        if self.stack.currentIndex() < self.stack.count() - 1:
            self.stack.setCurrentIndex(self.stack.currentIndex() + 1)
            self._sync_step()
            return
        self.config.mode = "observe"
        self.config.qmt.launcher = self.launcher_edit.text().strip()
        self.config.qmt.userdata_directory = self.userdata_edit.text().strip()
        self.config.qmt.log_directory = self.log_edit.text().strip()
        self.config.probe.python_executable = self.probe_python_edit.text().strip()
        self.config.probe.xtquant_parent = self.xtquant_edit.text().strip()
        self.config.rocket.log_directory = self.rocket_log_edit.text().strip()
        try:
            save_config(self.config, self.config_path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.accept()
