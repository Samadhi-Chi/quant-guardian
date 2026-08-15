from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QModelIndex, QObject, QSize, Qt, QTime, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from quant_guardian.config import AppConfig, save_config
from quant_guardian.domain.models import GuardianState
from quant_guardian.notifications import Notification
from quant_guardian.service import GuardianService, ServiceStatus
from quant_guardian.ui.design_system import (
    DARK,
    LIGHT,
    build_stylesheet,
    icon_pixmap,
    line_icon,
)
from quant_guardian.ui.dialogs import (
    FirstRunDialog,
    ManualConfirmDialog,
    QuantclassRestartConfirmDialog,
    RestartConfirmDialog,
)
from quant_guardian.ui.event_model import EventTableModel, OperationTableModel
from quant_guardian.ui.widgets import (
    ComponentGroupCard,
    HealthSample,
    HealthTimelineWidget,
    LatencyChartWidget,
    MetricCard,
    NavButton,
    PillLabel,
    SettingsNavButton,
    StateHero,
    TaskOutcomeChartWidget,
    compute_trend_metrics,
    sample_from_document,
    sample_from_status,
)


class ServiceBridge(QObject):
    status_received = Signal(object)
    notification_received = Signal(object)
    events_received = Signal(int, bool, object)
    trends_received = Signal(int, object)
    operations_received = Signal(int, bool, object)
    operation_detail_received = Signal(int, object)
    operation_finished = Signal(str, object, str)


def _tray_icon(color: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(2, 2, 28, 28, 8, 8)
    painter.drawPixmap(7, 7, icon_pixmap("shield_check", "#FFFFFF", 18))
    painter.end()
    return QIcon(pixmap)


def _button(text: str, icon_name: str | None = None, *, variant: str | None = None) -> QPushButton:
    button = QPushButton(text)
    if icon_name:
        button.setIcon(line_icon(icon_name, 18))
        button.setIconSize(QSize(17, 17))
    if variant:
        button.setProperty("variant", variant)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _page_heading(title: str, subtitle: str) -> tuple[QWidget, QHBoxLayout]:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    copy_layout = QVBoxLayout()
    copy_layout.setSpacing(3)
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    caption = QLabel(subtitle)
    caption.setObjectName("pageSubtitle")
    caption.setWordWrap(True)
    copy_layout.addWidget(heading)
    copy_layout.addWidget(caption)
    layout.addLayout(copy_layout, 1)
    return widget, layout


def _scroll_page() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.viewport().setObjectName("scrollViewport")
    body = QWidget()
    body.setObjectName("scrollBody")
    layout = QVBoxLayout(body)
    layout.setContentsMargins(22, 20, 22, 24)
    layout.setSpacing(14)
    scroll.setWidget(body)
    return scroll, body, layout


def _section_frame(title: str, caption: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("formSection")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 16)
    layout.setSpacing(9)
    title_label = QLabel(title)
    title_label.setObjectName("sectionTitle")
    layout.addWidget(title_label)
    if caption:
        caption_label = QLabel(caption)
        caption_label.setObjectName("sectionCaption")
        caption_label.setWordWrap(True)
        layout.addWidget(caption_label)
    return frame, layout


def _form() -> QFormLayout:
    layout = QFormLayout()
    layout.setContentsMargins(0, 3, 0, 0)
    layout.setHorizontalSpacing(18)
    layout.setVerticalSpacing(10)
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    return layout


def _line(value: object) -> QLineEdit:
    editor = QLineEdit(str(value or ""))
    editor.setClearButtonEnabled(True)
    return editor


def _spin(value: int, minimum: int, maximum: int, suffix: str = "") -> QSpinBox:
    editor = QSpinBox()
    editor.setRange(minimum, maximum)
    editor.setValue(int(value))
    editor.setSuffix(suffix)
    return editor


def _double_spin(value: float, minimum: float, maximum: float, suffix: str = "") -> QDoubleSpinBox:
    editor = QDoubleSpinBox()
    editor.setRange(minimum, maximum)
    editor.setDecimals(1)
    editor.setSingleStep(1.0)
    editor.setValue(float(value))
    editor.setSuffix(suffix)
    return editor


def _time_editor(value: str) -> QTimeEdit:
    editor = QTimeEdit()
    editor.setDisplayFormat("HH:mm")
    parsed = QTime.fromString(value, "HH:mm")
    editor.setTime(parsed if parsed.isValid() else QTime(0, 0))
    return editor


class MainWindow(QMainWindow):
    """Three-page operational UI. Disk-backed monitoring data is loaded off-thread."""

    def __init__(
        self,
        service: GuardianService,
        config: AppConfig,
        config_path: Path,
        *,
        enable_tray: bool = True,
        show_onboarding: bool = False,
    ) -> None:
        super().__init__()
        self.service = service
        self.config = config
        self.config_path = config_path
        self.bridge = ServiceBridge()
        self.bridge.status_received.connect(self.apply_status)
        self.bridge.notification_received.connect(self.show_notification)
        self.bridge.events_received.connect(self._apply_events)
        self.bridge.trends_received.connect(self._apply_trends)
        self.bridge.operations_received.connect(self._apply_operations)
        self.bridge.operation_detail_received.connect(self._apply_operation_detail)
        self.bridge.operation_finished.connect(self._operation_finished)
        self.service.subscribe(self.bridge.status_received.emit)
        self.service.notifications.subscribe(self.bridge.notification_received.emit)
        self._allow_close = False
        self._dark = False
        self._last_status = service.status
        self._history: deque[HealthSample] = deque(maxlen=config.monitoring.max_chart_points)
        self._event_generation = 0
        self._trend_generation = 0
        self._operations_generation = 0
        self._operation_detail_generation = 0
        self._operations_loading = False
        self._operations_have_more = True
        self._event_loading = False
        self._event_has_more = True
        self._monitor_range = "today"
        self._operation_in_progress = False
        self.tray: QSystemTrayIcon | None = None

        self.setWindowTitle("Quant Guardian")
        self.setMinimumSize(820, 620)
        self.resize(1120, 820)
        self._build_ui()
        self._apply_theme(False)
        if enable_tray:
            self._build_tray()
        self.apply_status(service.status)
        if show_onboarding:
            QTimer.singleShot(120, self.open_onboarding)

    # ----- Shell and navigation ---------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_top_bar())
        self.page_stack = QStackedWidget()
        self.status_page = self._build_status_page()
        self.monitoring_page = self._build_monitoring_page()
        self.settings_page = self._build_settings_page()
        for page in (self.status_page, self.monitoring_page, self.settings_page):
            self.page_stack.addWidget(page)
        root_layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(root)

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setFixedHeight(64)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 8, 14, 8)
        layout.setSpacing(14)
        brand_icon = QLabel()
        brand_icon.setPixmap(icon_pixmap("shield_check", LIGHT["indigo"], 25))
        brand_icon.setFixedSize(32, 32)
        layout.addWidget(brand_icon)
        brand_copy = QVBoxLayout()
        brand_copy.setSpacing(0)
        title = QLabel("Quant Guardian")
        title.setObjectName("brandTitle")
        caption = QLabel("QMT 与交易系统守护")
        caption.setObjectName("brandCaption")
        brand_copy.addWidget(title)
        brand_copy.addWidget(caption)
        layout.addLayout(brand_copy)
        layout.addSpacing(14)
        self.nav_buttons: list[NavButton] = []
        for index, (text, icon_name) in enumerate(
            (("状态", "overview"), ("监控", "trend"), ("设置", "settings"))
        ):
            button = NavButton(text, icon_name)
            button.clicked.connect(lambda _checked=False, value=index: self.switch_page(value))
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        layout.addStretch()
        self.top_state_pill = PillLabel("启动验证", "info")
        layout.addWidget(self.top_state_pill)
        self.theme_button = QToolButton()
        self.theme_button.setObjectName("iconButton")
        self.theme_button.setIcon(line_icon("moon", 18))
        self.theme_button.setIconSize(QSize(18, 18))
        self.theme_button.setToolTip("切换深色主题")
        self.theme_button.clicked.connect(self.toggle_theme)
        layout.addWidget(self.theme_button)
        return bar

    def switch_page(self, index: int) -> None:
        if not 0 <= index < self.page_stack.count():
            return
        self.page_stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        if index == 1:
            self._request_events(reset=True)
            self._request_trends()
            self._request_operations(reset=True)

    # ----- Status -----------------------------------------------------------

    def _build_status_page(self) -> QScrollArea:
        scroll, _body, layout = _scroll_page()
        heading, heading_layout = _page_heading(
            "当前状态",
            "现在是否正常、关键组件是否可用，以及是否需要你处理。",
        )
        layout.addWidget(heading)
        self.state_hero = StateHero()
        self.state_hero.action_button.clicked.connect(self._run_hero_action)
        layout.addWidget(self.state_hero)
        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.qmt_check_button = _button("检测", "refresh")
        self.qmt_check_button.setToolTip("立即检测QMT进程、XTQuant连接与只读接口")
        self.qmt_check_button.clicked.connect(lambda: self._run_check("qmt"))
        self.qmt_restart_button = _button("重启", "repair")
        self.qmt_restart_button.setToolTip("确认后人工重启QMT；不受观察模式限制")
        self.qmt_restart_button.clicked.connect(self.manual_restart)
        self.restart_qmt_button = self.qmt_restart_button
        self.qmt_card = ComponentGroupCard(
            "QMT API",
            "进程、XTQuant 连接、账户与只读委托汇总",
            "terminal",
            (
                ("qmt_api.process", "QMT 进程", "process"),
                ("qmt_api.xtquant", "XTQuant 连接", "link"),
                ("qmt_api.account", "账户只读查询", "account"),
                ("qmt_api.trading_snapshot", "委托与持仓汇总", "orders"),
            ),
            actions=(self.qmt_check_button, self.qmt_restart_button),
        )
        self.trade_check_button = _button("检测", "refresh")
        self.trade_check_button.setToolTip("立即检测Fuel、选股内核与Rocket状态")
        self.trade_check_button.clicked.connect(lambda: self._run_check("trade"))
        self.trade_restart_button = _button("重启", "repair")
        self.trade_restart_button.setToolTip(
            "确认后人工重启Quantclass客户端；不会主动终止Fuel、Zeus或Rocket"
        )
        self.trade_restart_button.clicked.connect(self.manual_restart_trade_system)
        self.trade_card = ComponentGroupCard(
            "Trade System",
            "Fuel 数据、Aqua/Zeus 选股与 Rocket 下单",
            "layers",
            (
                ("trade_system.data", "数据内核 · Fuel", "database"),
                (
                    "trade_system.selection",
                    f"选股内核 · {self.config.trade_system.selection_engine.title()}",
                    "terminal",
                ),
                ("trade_system.order", "下单内核 · Rocket", "rocket"),
            ),
            actions=(self.trade_check_button, self.trade_restart_button),
        )
        cards.addWidget(self.qmt_card, 1)
        cards.addWidget(self.trade_card, 1)
        layout.addLayout(cards)
        layout.addStretch()
        return scroll

    # ----- Monitoring -------------------------------------------------------

    def _build_monitoring_page(self) -> QScrollArea:
        scroll, _body, layout = _scroll_page()
        heading, heading_layout = _page_heading(
            "监控",
            "跨重启保留的健康趋势、任务结果与可分页事件证据。",
        )
        self.range_buttons: dict[str, QPushButton] = {}
        for key, label in (("1h", "近 1 小时"), ("today", "今天"), ("7d", "近 7 日")):
            button = _button(label, variant="ghost")
            button.setCheckable(True)
            button.setChecked(key == self._monitor_range)
            button.clicked.connect(lambda _checked=False, value=key: self._set_monitor_range(value))
            self.range_buttons[key] = button
            heading_layout.addWidget(button)
        export = _button("导出诊断", "download", variant="ghost")
        export.clicked.connect(self.export_diagnostics)
        heading_layout.addWidget(export)
        layout.addWidget(heading)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        self.availability_metric = MetricCard("整体可用率", "—", "QMT API 主链")
        self.latency_metric = MetricCard("QMT API 延迟", "—", "平均 / P95")
        self.trade_metric = MetricCard("Trade System 成功率", "—", "含空闲有效样本")
        self.incident_metric = MetricCard("异常事件", "—", "所选时间范围")
        for column, card in enumerate(
            (self.availability_metric, self.latency_metric, self.trade_metric, self.incident_metric)
        ):
            metrics.addWidget(card, 0, column)
        layout.addLayout(metrics)

        self.health_timeline = HealthTimelineWidget()
        layout.addWidget(self.health_timeline)
        charts = QHBoxLayout()
        charts.setSpacing(14)
        self.latency_chart = LatencyChartWidget()
        self.task_chart = TaskOutcomeChartWidget()
        charts.addWidget(self.latency_chart, 1)
        charts.addWidget(self.task_chart, 1)
        layout.addLayout(charts)

        operation_frame, operation_layout = _section_frame(
            "操作与恢复",
            "统计以稳定验证为准；启动命令成功但 XTQuant 未恢复，不计为恢复成功。自动恢复仅作用于 QMT。",
        )
        operation_metrics = QGridLayout()
        operation_metrics.setHorizontalSpacing(12)
        self.recovery_success_metric = MetricCard(
            "恢复事件成功率", "—", "按故障事件统计"
        )
        self.restart_attempt_metric = MetricCard(
            "QMT 重启尝试", "—", "自动 / 人工"
        )
        self.verified_restart_metric = MetricCard(
            "稳定验证通过", "—", "按重启尝试统计"
        )
        self.mttr_metric = MetricCard(
            "恢复耗时 MTTR", "—", "中位数 / P95"
        )
        for column, card in enumerate(
            (
                self.recovery_success_metric,
                self.restart_attempt_metric,
                self.verified_restart_metric,
                self.mttr_metric,
            )
        ):
            operation_metrics.addWidget(card, 0, column)
        operation_layout.addLayout(operation_metrics)

        operation_filters = QHBoxLayout()
        self.operation_search = QLineEdit()
        self.operation_search.setPlaceholderText("搜索操作、事件编号或摘要")
        self.operation_search.setClearButtonEnabled(True)
        self.operation_type = QComboBox()
        for label, value in (
            ("全部操作", "all"),
            ("重启 QMT", "qmt_restart"),
            ("立即检测", "manual_check"),
            ("监控线程恢复", "guardian_worker_restart"),
            ("恢复控制", "recovery_control"),
            ("设置变更", "settings_change"),
            ("诊断导出", "diagnostic_export"),
            ("人工重启 QuantClass", "quantclass_restart"),
        ):
            self.operation_type.addItem(label, value)
        self.operation_initiator = QComboBox()
        for label, value in (
            ("全部发起方", "all"),
            ("自动恢复", "automatic"),
            ("人工", "manual"),
            ("看门狗", "watchdog"),
        ):
            self.operation_initiator.addItem(label, value)
        self.operation_status = QComboBox()
        for label, value in (
            ("全部结果", "all"),
            ("验证成功", "succeeded"),
            ("失败", "failed"),
            ("验证中", "verifying"),
            ("执行中", "in_progress"),
            ("已阻断", "blocked"),
        ):
            self.operation_status.addItem(label, value)
        self.operation_context = QComboBox()
        for label, value in (
            ("全部环境", "all"),
            ("正式运行", "production"),
            ("历史回填", "legacy"),
            ("演练", "drill"),
        ):
            self.operation_context.addItem(label, value)
        operation_refresh = _button("刷新", "refresh", variant="ghost")
        operation_refresh.clicked.connect(
            lambda: self._request_operations(reset=True)
        )
        operation_filters.addWidget(self.operation_search, 1)
        operation_filters.addWidget(self.operation_type)
        operation_filters.addWidget(self.operation_initiator)
        operation_filters.addWidget(self.operation_status)
        operation_filters.addWidget(self.operation_context)
        operation_filters.addWidget(operation_refresh)
        operation_layout.addLayout(operation_filters)
        self.operation_debounce = QTimer(self)
        self.operation_debounce.setSingleShot(True)
        self.operation_debounce.setInterval(250)
        self.operation_debounce.timeout.connect(
            lambda: self._request_operations(reset=True)
        )
        self.operation_search.textChanged.connect(
            lambda _value: self.operation_debounce.start()
        )
        for combo in (
            self.operation_type,
            self.operation_initiator,
            self.operation_status,
            self.operation_context,
        ):
            combo.currentIndexChanged.connect(
                lambda _value: self.operation_debounce.start()
            )

        operation_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.operation_table = QTableView()
        self.operation_table.setObjectName("operationTable")
        self.operation_model = OperationTableModel(self.operation_table)
        self.operation_table.setModel(self.operation_model)
        self.operation_table.setSelectionBehavior(
            QTableView.SelectionBehavior.SelectRows
        )
        self.operation_table.setSelectionMode(
            QTableView.SelectionMode.SingleSelection
        )
        self.operation_table.setAlternatingRowColors(True)
        self.operation_table.setSortingEnabled(False)
        self.operation_table.verticalHeader().setVisible(False)
        self.operation_table.setMinimumHeight(315)
        operation_header = self.operation_table.horizontalHeader()
        for section, width in ((0, 145), (1, 135), (2, 85), (3, 90), (4, 80)):
            operation_header.setSectionResizeMode(
                section, QHeaderView.ResizeMode.Fixed
            )
            operation_header.resizeSection(section, width)
        operation_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.operation_table.clicked.connect(self._show_operation_index)
        self.operation_table.verticalScrollBar().valueChanged.connect(
            self._maybe_load_more_operations
        )
        operation_splitter.addWidget(self.operation_table)
        self.operation_detail = self._build_operation_detail()
        operation_splitter.addWidget(self.operation_detail)
        operation_splitter.setSizes([720, 330])
        operation_splitter.setStretchFactor(0, 2)
        operation_splitter.setStretchFactor(1, 1)
        operation_layout.addWidget(operation_splitter)
        layout.addWidget(operation_frame)

        event_frame, event_layout = _section_frame(
            "事件流",
            "首次 200 条，滚动继续加载；点击一行在右侧查看可读证据。",
        )
        filters = QHBoxLayout()
        self.event_search = QLineEdit()
        self.event_search.setPlaceholderText("搜索摘要、类型或证据")
        self.event_search.setClearButtonEnabled(True)
        self.event_component = QComboBox()
        self.event_component.addItem("全部组件", "all")
        self.event_component.addItem("QMT API", "qmt_api")
        self.event_component.addItem("Trade System", "trade_system")
        self.event_component.addItem("Guardian", "quant_guardian")
        self.event_severity = QComboBox()
        self.event_severity.addItem("全部级别", "all")
        self.event_severity.addItem("信息", "info")
        self.event_severity.addItem("警告", "warning")
        self.event_severity.addItem("严重", "critical")
        refresh = _button("刷新", "refresh", variant="ghost")
        refresh.clicked.connect(lambda: self._request_events(reset=True))
        filters.addWidget(self.event_search, 1)
        filters.addWidget(self.event_component)
        filters.addWidget(self.event_severity)
        filters.addWidget(refresh)
        event_layout.addLayout(filters)
        self.event_debounce = QTimer(self)
        self.event_debounce.setSingleShot(True)
        self.event_debounce.setInterval(250)
        self.event_debounce.timeout.connect(lambda: self._request_events(reset=True))
        self.event_search.textChanged.connect(lambda _value: self.event_debounce.start())
        self.event_component.currentIndexChanged.connect(lambda _value: self.event_debounce.start())
        self.event_severity.currentIndexChanged.connect(lambda _value: self.event_debounce.start())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.event_table = QTableView()
        self.event_table.setObjectName("eventTable")
        self.event_model = EventTableModel(self.event_table)
        self.event_table.setModel(self.event_model)
        self.event_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.event_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.event_table.setAlternatingRowColors(True)
        self.event_table.setSortingEnabled(False)
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.setMinimumHeight(340)
        header = self.event_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, 145)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 105)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(2, 145)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(3, 68)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.event_table.clicked.connect(self._show_event_index)
        self.event_table.verticalScrollBar().valueChanged.connect(self._maybe_load_more_events)
        splitter.addWidget(self.event_table)
        self.event_detail = self._build_event_detail()
        splitter.addWidget(self.event_detail)
        splitter.setSizes([720, 330])
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        event_layout.addWidget(splitter)
        layout.addWidget(event_frame)
        return scroll

    def _build_operation_detail(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("stateDetail")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(15, 13, 15, 14)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        self.operation_detail_title = QLabel("选择一项操作")
        self.operation_detail_title.setObjectName("sectionTitle")
        self.operation_detail_status = PillLabel("结果", "neutral")
        title_row.addWidget(self.operation_detail_title, 1)
        title_row.addWidget(self.operation_detail_status)
        layout.addLayout(title_row)
        self.operation_detail_summary = QLabel(
            "这里会显示最终结果、恢复验证、关联故障和每一步证据。"
        )
        self.operation_detail_summary.setObjectName("cardCaption")
        self.operation_detail_summary.setWordWrap(True)
        layout.addWidget(self.operation_detail_summary)
        self.operation_detail_metadata = QPlainTextEdit()
        self.operation_detail_metadata.setReadOnly(True)
        self.operation_detail_metadata.setPlaceholderText("暂无操作详情")
        self.operation_detail_metadata.setMaximumHeight(125)
        layout.addWidget(self.operation_detail_metadata)
        steps_label = QLabel("执行与验证步骤")
        steps_label.setObjectName("fieldLabel")
        layout.addWidget(steps_label)
        self.operation_detail_steps = QPlainTextEdit()
        self.operation_detail_steps.setReadOnly(True)
        self.operation_detail_steps.setPlaceholderText("暂无关联事件")
        layout.addWidget(self.operation_detail_steps, 1)
        self.operation_raw_toggle = QCheckBox("显示原始 JSON")
        self.operation_raw_toggle.toggled.connect(
            lambda checked: self.operation_raw.setVisible(checked)
        )
        layout.addWidget(self.operation_raw_toggle)
        self.operation_raw = QPlainTextEdit()
        self.operation_raw.setReadOnly(True)
        self.operation_raw.setVisible(False)
        self.operation_raw.setMaximumHeight(150)
        layout.addWidget(self.operation_raw)
        return frame

    def _build_event_detail(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("stateDetail")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(15, 13, 15, 14)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        self.event_detail_title = QLabel("选择一个事件")
        self.event_detail_title.setObjectName("sectionTitle")
        self.event_detail_severity = PillLabel("证据", "neutral")
        title_row.addWidget(self.event_detail_title, 1)
        title_row.addWidget(self.event_detail_severity)
        layout.addLayout(title_row)
        self.event_detail_summary = QLabel("可读摘要、组件和结构化证据会显示在这里。")
        self.event_detail_summary.setObjectName("cardCaption")
        self.event_detail_summary.setWordWrap(True)
        layout.addWidget(self.event_detail_summary)
        self.event_detail_evidence = QPlainTextEdit()
        self.event_detail_evidence.setReadOnly(True)
        self.event_detail_evidence.setPlaceholderText("暂无结构化证据")
        layout.addWidget(self.event_detail_evidence, 1)
        self.event_raw_toggle = QCheckBox("显示原始 JSON")
        self.event_raw_toggle.toggled.connect(self._toggle_event_raw)
        layout.addWidget(self.event_raw_toggle)
        self.event_raw = QPlainTextEdit()
        self.event_raw.setReadOnly(True)
        self.event_raw.setVisible(False)
        self.event_raw.setMaximumHeight(150)
        layout.addWidget(self.event_raw)
        return frame

    # ----- Settings ---------------------------------------------------------

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("scrollBody")
        root = QHBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        nav = QFrame()
        nav.setObjectName("settingsSidebar")
        nav.setFixedWidth(220)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(14, 20, 14, 18)
        nav_layout.setSpacing(6)
        label = QLabel("设置")
        label.setObjectName("settingsTitle")
        nav_layout.addWidget(label)
        nav_layout.addSpacing(10)
        self.settings_nav: list[SettingsNavButton] = []
        for index, (text, icon) in enumerate(
            (
                ("运行与安全", "shield"),
                ("路径与组件", "folder"),
                ("监控频率", "clock"),
                ("交易日历", "calendar"),
                ("通知与数据", "notification"),
            )
        ):
            button = SettingsNavButton(text, icon)
            button.clicked.connect(lambda _checked=False, value=index: self._switch_settings(value))
            self.settings_nav.append(button)
            nav_layout.addWidget(button)
        self.settings_nav[0].setChecked(True)
        nav_layout.addStretch()
        save = _button("保存设置", "check", variant="primary")
        save.clicked.connect(self.save_settings)
        nav_layout.addWidget(save)
        root.addWidget(nav)
        self.settings_stack = QStackedWidget()
        self.settings_stack.addWidget(self._settings_safety())
        self.settings_stack.addWidget(self._settings_paths())
        self.settings_stack.addWidget(self._settings_monitoring())
        self.settings_stack.addWidget(self._settings_calendar())
        self.settings_stack.addWidget(self._settings_notifications())
        root.addWidget(self.settings_stack, 1)
        return page

    def _settings_scroll(self, title: str, subtitle: str) -> tuple[QScrollArea, QVBoxLayout]:
        scroll, _body, layout = _scroll_page()
        heading, _ = _page_heading(title, subtitle)
        layout.addWidget(heading)
        return scroll, layout

    def _settings_safety(self) -> QScrollArea:
        scroll, layout = self._settings_scroll(
            "运行与安全",
            "自动恢复动作由模式、独立授权文件和安全闸门共同控制。",
        )
        frame, content = _section_frame("恢复授权", "观察模式永远不会关闭或启动 QMT。")
        form = _form()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("观察模式", "observe")
        self.mode_combo.addItem("自动恢复模式", "recover")
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(self.config.mode)))
        self.allow_idle_recovery = QCheckBox("允许非活跃时段经三次复核后恢复 QMT")
        self.allow_idle_recovery.setChecked(self.config.monitoring.allow_idle_recovery)
        self.allow_qmt_with_rocket = QCheckBox("Rocket 活跃时允许受控重启 QMT")
        self.allow_qmt_with_rocket.setChecked(self.config.recovery.allow_qmt_restart_while_rocket_active)
        self.manual_rocket_resume = QCheckBox("QMT 恢复后必须人工确认 Rocket")
        self.manual_rocket_resume.setChecked(self.config.recovery.require_manual_rocket_resume)
        form.addRow("运行模式", self.mode_combo)
        form.addRow("非活跃恢复", self.allow_idle_recovery)
        form.addRow("Rocket 安全闸门", self.allow_qmt_with_rocket)
        form.addRow("恢复后确认", self.manual_rocket_resume)
        content.addLayout(form)
        layout.addWidget(frame)
        limits, limits_layout = _section_frame("恢复限制", "退避与次数上限可以避免故障循环干扰实盘。")
        limit_form = _form()
        self.max_30 = _spin(self.config.recovery.max_attempts_per_30_minutes, 1, 20, " 次")
        self.max_day = _spin(self.config.recovery.max_attempts_per_day, 1, 50, " 次")
        self.graceful_close = _spin(self.config.recovery.graceful_close_seconds, 5, 120, " 秒")
        self.backoff = _line(", ".join(str(v) for v in self.config.recovery.backoff_seconds))
        limit_form.addRow("30 分钟上限", self.max_30)
        limit_form.addRow("每日上限", self.max_day)
        limit_form.addRow("优雅关闭等待", self.graceful_close)
        limit_form.addRow("退避秒数", self.backoff)
        limits_layout.addLayout(limit_form)
        layout.addWidget(limits)
        layout.addStretch()
        return scroll

    def _settings_paths(self) -> QScrollArea:
        scroll, layout = self._settings_scroll(
            "QMT API 与 Trade System 路径",
            "Trade System 只读监控 Fuel、Aqua、Zeus 和 Rocket，不会启动或修复它们。",
        )
        qmt, qmt_layout = _section_frame("QMT API")
        qmt_form = _form()
        self.qmt_launcher = _line(self.config.qmt.launcher)
        self.qmt_workdir = _line(self.config.qmt.working_directory)
        self.qmt_userdata = _line(self.config.qmt.userdata_directory)
        self.qmt_logdir = _line(self.config.qmt.log_directory)
        self.probe_python = _line(self.config.probe.python_executable)
        self.xtquant_parent = _line(self.config.probe.xtquant_parent)
        qmt_form.addRow("官方启动器", self.qmt_launcher)
        qmt_form.addRow("工作目录", self.qmt_workdir)
        qmt_form.addRow("userdata_mini", self.qmt_userdata)
        qmt_form.addRow("QMT 日志", self.qmt_logdir)
        qmt_form.addRow("探针 Python", self.probe_python)
        qmt_form.addRow("XTQuant 目录", self.xtquant_parent)
        qmt_layout.addLayout(qmt_form)
        layout.addWidget(qmt)
        trade, trade_layout = _section_frame("Trade System")
        trade_form = _form()
        self.trade_enabled = QCheckBox("启用 Trade System 只读监控")
        self.trade_enabled.setChecked(self.config.trade_system.enabled)
        self.selection_engine = QComboBox()
        self.selection_engine.addItem("Zeus（当前）", "zeus")
        self.selection_engine.addItem("Aqua", "aqua")
        self.selection_engine.setCurrentIndex(
            max(
                0,
                self.selection_engine.findData(
                    str(self.config.trade_system.selection_engine).casefold()
                ),
            )
        )
        self.trade_root = _line(self.config.trade_system.data_root)
        self.quantclass_executable = _line(
            self.config.trade_system.client_executable
        )
        self.quantclass_config = _line(self.config.trade_system.quantclass_config)
        self.fuel_status = _line(self.config.trade_system.fuel_status_file)
        self.aqua_log = _line(self.config.trade_system.aqua_log_file)
        self.zeus_log = _line(self.config.trade_system.zeus_log_file)
        self.rocket_log = _line(self.config.trade_system.rocket_log_directory)
        trade_form.addRow("组件", self.trade_enabled)
        trade_form.addRow("当前选股内核", self.selection_engine)
        trade_form.addRow("数据根目录", self.trade_root)
        trade_form.addRow("Quantclass 客户端", self.quantclass_executable)
        trade_form.addRow("Quantclass 配置", self.quantclass_config)
        trade_form.addRow("Fuel 状态", self.fuel_status)
        trade_form.addRow("Aqua 日志", self.aqua_log)
        trade_form.addRow("Zeus 日志", self.zeus_log)
        trade_form.addRow("Rocket 日志目录", self.rocket_log)
        trade_layout.addLayout(trade_form)
        layout.addWidget(trade)
        layout.addStretch()
        return scroll

    def _settings_monitoring(self) -> QScrollArea:
        scroll, layout = self._settings_scroll(
            "监控频率",
            "交易日 08:30–16:30 保持 5 秒主探针；其他时间每小时检查，异常时临时加速复核。",
        )
        schedule, schedule_layout = _section_frame("分时调度")
        form = _form()
        self.active_start = _time_editor(self.config.monitoring.active_start)
        self.active_end = _time_editor(self.config.monitoring.active_end)
        self.active_interval = _double_spin(self.config.monitoring.active_interval_seconds, 1, 300, " 秒")
        self.idle_interval = _spin(int(self.config.monitoring.idle_interval_seconds), 60, 86400, " 秒")
        self.anomaly_retry = _double_spin(self.config.monitoring.anomaly_retry_seconds, 1, 300, " 秒")
        self.anomaly_checks = _spin(self.config.monitoring.anomaly_confirmation_checks, 2, 10, " 次")
        form.addRow("活跃时段开始", self.active_start)
        form.addRow("活跃时段结束", self.active_end)
        form.addRow("活跃检查频率", self.active_interval)
        form.addRow("非活跃检查频率", self.idle_interval)
        form.addRow("异常复核间隔", self.anomaly_retry)
        form.addRow("一致失败次数", self.anomaly_checks)
        schedule_layout.addLayout(form)
        probes, probes_layout = _section_frame("探针与验证")
        probe_form = _form()
        self.business_interval = _double_spin(self.config.monitoring.business_summary_interval_seconds, 30, 3600, " 秒")
        self.business_timeout = _double_spin(self.config.monitoring.business_summary_timeout_seconds, 0.5, 30, " 秒")
        self.health_timeout = _double_spin(self.config.probe.timeout_seconds, 1, 30, " 秒")
        self.failure_threshold = _spin(self.config.thresholds.failure_threshold, 2, 20, " 次")
        self.failure_window = _spin(self.config.thresholds.failure_window_seconds, 5, 600, " 秒")
        self.startup_grace = _spin(self.config.thresholds.startup_grace_seconds, 30, 900, " 秒")
        self.verify_successes = _spin(self.config.thresholds.verify_successes, 2, 20, " 次")
        self.verify_span = _spin(self.config.thresholds.verify_min_span_seconds, 0, 600, " 秒")
        self.verification_timeout = _spin(
            self.config.thresholds.verification_timeout_seconds,
            60,
            900,
            " 秒",
        )
        probe_form.addRow("业务汇总频率", self.business_interval)
        probe_form.addRow("业务汇总超时", self.business_timeout)
        probe_form.addRow("健康探针超时", self.health_timeout)
        probe_form.addRow("活跃失败阈值", self.failure_threshold)
        probe_form.addRow("失败窗口", self.failure_window)
        probe_form.addRow("启动宽限", self.startup_grace)
        probe_form.addRow("恢复连续成功", self.verify_successes)
        probe_form.addRow("最短验证跨度", self.verify_span)
        probe_form.addRow("稳定验证超时", self.verification_timeout)
        probes_layout.addLayout(probe_form)
        calendar, calendar_layout = _section_frame(
            "交易日历",
            "内置 2026 年官方休市表，并由 QMT 交易日只读查询每日交叉验证。手工开市/休市覆盖优先级最高。",
        )
        calendar_button = _button("管理手工覆盖", "calendar", variant="ghost")
        calendar_button.clicked.connect(lambda: self._switch_settings(3))
        calendar_layout.addWidget(calendar_button, 0, Qt.AlignmentFlag.AlignLeft)
        columns = QHBoxLayout()
        columns.setSpacing(14)
        left_column = QVBoxLayout()
        left_column.setSpacing(14)
        left_column.addWidget(schedule)
        left_column.addWidget(calendar)
        left_column.addStretch()
        right_column = QVBoxLayout()
        right_column.addWidget(probes)
        right_column.addStretch()
        columns.addLayout(left_column, 1)
        columns.addLayout(right_column, 1)
        layout.addLayout(columns)
        layout.addStretch()
        return scroll

    def _settings_calendar(self) -> QScrollArea:
        scroll, layout = self._settings_scroll(
            "交易日历",
            "手工覆盖优先级最高；内置官方休市表与 QMT 交易日查询交叉验证。",
        )
        status, status_layout = _section_frame("日历来源")
        note = QLabel(
            "2026 年内置上交所官方休市安排。每日通过 QMT get_trading_dates('SH') 只读交叉验证并缓存；"
            "超出覆盖年份时显示警告并按工作日保守高频检查。"
        )
        note.setWordWrap(True)
        note.setObjectName("cardCaption")
        status_layout.addWidget(note)
        layout.addWidget(status)
        override, override_layout = _section_frame("手工覆盖", "每行一个 YYYY-MM-DD 日期。")
        form = _form()
        self.manual_closed = QPlainTextEdit("\n".join(self.config.trading.manual_closed_dates))
        self.manual_closed.setMaximumHeight(130)
        self.manual_open = QPlainTextEdit("\n".join(self.config.trading.manual_open_dates))
        self.manual_open.setMaximumHeight(130)
        form.addRow("强制休市", self.manual_closed)
        form.addRow("强制开市", self.manual_open)
        override_layout.addLayout(form)
        layout.addWidget(override)
        layout.addStretch()
        return scroll

    def _settings_notifications(self) -> QScrollArea:
        scroll, layout = self._settings_scroll(
            "通知与数据",
            "JSONL 是不可变诊断记录，SQLite 是可重建的分页与趋势索引。",
        )
        frame, content = _section_frame("桌面通知")
        form = _form()
        self.desktop_notifications = QCheckBox("启用桌面通知")
        self.desktop_notifications.setChecked(self.config.notifications.desktop_enabled)
        self.sound_critical = QCheckBox("严重事件播放提示音")
        self.sound_critical.setChecked(self.config.notifications.sound_on_critical)
        self.notification_dedupe = _spin(self.config.notifications.dedupe_minutes, 1, 120, " 分钟")
        form.addRow("通知", self.desktop_notifications)
        form.addRow("声音", self.sound_critical)
        form.addRow("去重窗口", self.notification_dedupe)
        content.addLayout(form)
        layout.addWidget(frame)
        data, data_layout = _section_frame("诊断数据")
        data_form = _form()
        self.retention_days = _spin(self.config.diagnostics.retention_days, 1, 365, " 天")
        self.sqlite_enabled = QCheckBox("启用后台 SQLite 索引缓存（WAL）")
        self.sqlite_enabled.setChecked(self.config.diagnostics.sqlite_index_enabled)
        self.max_chart_points = _spin(self.config.monitoring.max_chart_points, 200, 10000, " 点")
        data_form.addRow("保留天数", self.retention_days)
        data_form.addRow("事件与趋势索引", self.sqlite_enabled)
        data_form.addRow("图表最大绘制点", self.max_chart_points)
        data_layout.addLayout(data_form)
        layout.addWidget(data)
        layout.addStretch()
        return scroll

    def _switch_settings(self, index: int) -> None:
        self.settings_stack.setCurrentIndex(index)
        self.settings_nav[index].setChecked(True)

    # ----- Data loading -----------------------------------------------------

    def _set_operation_busy(self, busy: bool) -> None:
        self._operation_in_progress = busy
        for button in (
            self.qmt_check_button,
            self.trade_check_button,
            self.trade_restart_button,
            self.state_hero.action_button,
        ):
            button.setEnabled(not busy)
        restart_pending = self._last_status.state in {
            GuardianState.RECOVERING,
            GuardianState.VERIFYING,
        }
        self.qmt_restart_button.setEnabled(not busy and not restart_pending)
        self.qmt_restart_button.setToolTip(
            "当前QMT重启仍在执行或稳定验证中"
            if restart_pending
            else "确认后人工重启QMT；不受观察模式限制"
        )

    def _run_check(self, source: str = "all") -> None:
        if self._operation_in_progress:
            return
        self._set_operation_busy(True)
        operation_name = (
            "check_qmt"
            if source == "qmt"
            else "check_trade"
            if source == "trade"
            else "check"
        )

        def worker() -> None:
            try:
                status = self.service.operator_check(source)
                self.bridge.operation_finished.emit(operation_name, status, "")
            except Exception as exc:  # noqa: BLE001 - surfaced to user
                self.bridge.operation_finished.emit(operation_name, None, str(exc))

        threading.Thread(
            target=worker,
            name=f"qg-ui-{operation_name}",
            daemon=True,
        ).start()

    def _set_monitor_range(self, key: str) -> None:
        self._monitor_range = key
        for value, button in self.range_buttons.items():
            button.setChecked(value == key)
        for chart in (self.health_timeline, self.latency_chart, self.task_chart):
            chart.set_range(key)
        self._request_trends()
        self._request_operations(reset=True)
        self._request_events(reset=True)

    def _monitor_bounds(self) -> tuple[datetime, datetime | None]:
        now = datetime.now().astimezone()
        since = (
            now - timedelta(hours=1)
            if self._monitor_range == "1h"
            else now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self._monitor_range == "today"
            else now - timedelta(days=7)
        )
        return since, None

    def _event_filters(self) -> tuple[str, str, str]:
        return (
            self.event_search.text().strip(),
            str(self.event_severity.currentData() or "all"),
            str(self.event_component.currentData() or "all"),
        )

    def _request_events(self, *, reset: bool) -> None:
        if self._event_loading and not reset:
            return
        if reset:
            self._event_generation += 1
            self._event_has_more = True
        if not self._event_has_more:
            return
        generation = self._event_generation
        offset = 0 if reset else self.event_model.rowCount()
        search, severity, component = self._event_filters()
        since, until = self._monitor_bounds()
        self._event_loading = True

        def worker() -> None:
            try:
                values = self.service.query_events(
                    limit=200,
                    offset=offset,
                    search=search,
                    severity=severity,
                    component=component,
                    since=since,
                    until=until,
                )
            except Exception:  # noqa: BLE001 - empty result keeps UI responsive
                values = []
            self.bridge.events_received.emit(generation, reset, values)

        threading.Thread(target=worker, name="qg-ui-events", daemon=True).start()

    @Slot(int, bool, object)
    def _apply_events(self, generation: int, reset: bool, values: object) -> None:
        if generation != self._event_generation:
            return
        rows = list(values) if isinstance(values, list) else []
        self._event_loading = False
        self._event_has_more = len(rows) == 200
        if reset:
            self.event_model.set_events(rows)
        else:
            self.event_model.append_events(rows)
        if reset:
            self.event_table.scrollToTop()

    def _maybe_load_more_events(self, value: int) -> None:
        scrollbar = self.event_table.verticalScrollBar()
        if scrollbar.maximum() - value <= 12:
            self._request_events(reset=False)

    def _request_trends(self) -> None:
        self._trend_generation += 1
        generation = self._trend_generation
        since, _until = self._monitor_bounds()

        def worker() -> None:
            try:
                values = self.service.trend_samples(
                    since=since,
                    limit=self.config.monitoring.max_chart_points,
                )
            except Exception:  # noqa: BLE001
                values = []
            self.bridge.trends_received.emit(generation, values)

        threading.Thread(target=worker, name="qg-ui-trends", daemon=True).start()

    def _operation_filters(self) -> tuple[str, str, str, str, str]:
        return (
            self.operation_search.text().strip(),
            str(self.operation_type.currentData() or "all"),
            str(self.operation_initiator.currentData() or "all"),
            str(self.operation_status.currentData() or "all"),
            str(self.operation_context.currentData() or "all"),
        )

    def _request_operations(self, *, reset: bool) -> None:
        if self._operations_loading and not reset:
            return
        if reset:
            self._operations_generation += 1
            self._operations_have_more = True
        if not self._operations_have_more:
            return
        generation = self._operations_generation
        offset = 0 if reset else self.operation_model.rowCount()
        search, operation_type, initiator, status, context = (
            self._operation_filters()
        )
        since, until = self._monitor_bounds()
        self._operations_loading = True

        def worker() -> None:
            try:
                rows = self.service.query_operations(
                    limit=200,
                    offset=offset,
                    since=since,
                    until=until,
                    operation_type=operation_type,
                    initiator=initiator,
                    status=status,
                    context=context,
                    search=search,
                )
                stats = (
                    self.service.operation_stats(
                        since=since,
                        until=until,
                        context=context,
                    )
                    if reset
                    else None
                )
                markers = (
                    self.service.query_operations(
                        limit=5000,
                        since=since,
                        until=until,
                        context=context,
                    )
                    if reset
                    else None
                )
                result = {"rows": rows, "stats": stats, "markers": markers}
            except Exception:  # noqa: BLE001 - keep the monitor page responsive
                result = {"rows": [], "stats": None, "markers": None}
            self.bridge.operations_received.emit(generation, reset, result)

        threading.Thread(
            target=worker,
            name="qg-ui-operations",
            daemon=True,
        ).start()

    @Slot(int, bool, object)
    def _apply_operations(
        self,
        generation: int,
        reset: bool,
        value: object,
    ) -> None:
        if generation != self._operations_generation:
            return
        document = value if isinstance(value, dict) else {}
        rows = document.get("rows")
        operations = list(rows) if isinstance(rows, list) else []
        self._operations_loading = False
        self._operations_have_more = len(operations) == 200
        if reset:
            self.operation_model.set_operations(operations)
            self.operation_table.scrollToTop()
        else:
            self.operation_model.append_operations(operations)
        stats = document.get("stats")
        if isinstance(stats, dict):
            self._apply_operation_stats(stats)
        markers = document.get("markers")
        if isinstance(markers, list):
            self.health_timeline.set_operation_markers(markers)

    @staticmethod
    def _duration_metric(value: object) -> str:
        if not isinstance(value, (int, float)):
            return "—"
        seconds = float(value) / 1000
        if seconds < 60:
            return f"{seconds:.1f} 秒"
        return f"{seconds / 60:.1f} 分"

    def _apply_operation_stats(self, stats: dict[str, object]) -> None:
        incidents = int(stats.get("recovery_incidents") or 0)
        resolved = int(stats.get("resolved_incidents") or 0)
        attempts = int(stats.get("qmt_restart_attempts") or 0)
        verified = int(stats.get("qmt_verified_attempts") or 0)
        recovery_rate = float(stats.get("recovery_success_rate") or 0)
        attempt_rate = float(stats.get("attempt_success_rate") or 0)
        automatic = int(stats.get("automatic_attempts") or 0)
        manual = int(stats.get("manual_attempts") or 0)
        repeated = int(stats.get("repeated_incidents") or 0)
        blocked = int(stats.get("blocked_operations") or 0)
        self.recovery_success_metric.set_value(
            f"{recovery_rate * 100:.1f}%" if incidents else "—",
            f"{resolved} / {incidents} 个故障事件",
        )
        self.restart_attempt_metric.set_value(
            str(attempts), f"自动 {automatic} · 人工 {manual}"
        )
        self.verified_restart_metric.set_value(
            f"{verified} / {attempts}" if attempts else "—",
            (
                f"尝试成功率 {attempt_rate * 100:.1f}%"
                if attempts
                else "尚无重启尝试"
            ),
        )
        median = self._duration_metric(stats.get("median_mttr_ms"))
        p95 = self._duration_metric(stats.get("p95_mttr_ms"))
        self.mttr_metric.set_value(
            median,
            f"P95 {p95} · 重复 {repeated} · 阻断 {blocked}",
        )

    def _maybe_load_more_operations(self, value: int) -> None:
        scrollbar = self.operation_table.verticalScrollBar()
        if scrollbar.maximum() - value <= 12:
            self._request_operations(reset=False)

    @Slot(QModelIndex)
    def _show_operation_index(self, index: QModelIndex) -> None:
        operation = self.operation_model.operation_at(index.row())
        if operation is None:
            return
        self._selected_operation = operation
        self._operation_detail_generation += 1
        generation = self._operation_detail_generation
        operation_id = str(operation.get("operation_id") or "")
        self.operation_detail_title.setText("正在加载操作详情…")
        self.operation_detail_steps.setPlainText("")

        def worker() -> None:
            try:
                detail = self.service.operation_detail(operation_id)
            except Exception:  # noqa: BLE001
                detail = {}
            self.bridge.operation_detail_received.emit(generation, detail)

        threading.Thread(
            target=worker,
            name="qg-ui-operation-detail",
            daemon=True,
        ).start()

    @Slot(int, object)
    def _apply_operation_detail(self, generation: int, value: object) -> None:
        if generation != self._operation_detail_generation:
            return
        detail = value if isinstance(value, dict) else {}
        operation = detail.get("operation")
        if not isinstance(operation, dict):
            operation = getattr(self, "_selected_operation", {})
        operation_type = str(operation.get("operation_type") or "操作")
        status = str(operation.get("status") or "unknown")
        title = OperationTableModel.OPERATION_NAMES.get(
            operation_type, operation_type
        )
        status_text = OperationTableModel.STATUS_NAMES.get(status, status)
        self.operation_detail_title.setText(title)
        self.operation_detail_status.setText(status_text)
        self.operation_detail_status.set_tone(
            "success"
            if status == "succeeded"
            else "danger"
            if status == "failed"
            else "warning"
            if status in {"blocked", "verifying", "in_progress"}
            else "neutral"
        )
        summary = str(operation.get("summary") or "暂无可读摘要")
        if status == "verifying":
            summary = f"启动步骤已完成，仍在等待 QMT API 稳定验证。\n{summary}"
        self.operation_detail_summary.setText(summary)
        metadata = (
            f"操作编号: {operation.get('operation_id') or '—'}\n"
            f"故障编号: {operation.get('incident_id') or '—'}\n"
            f"开始: {str(operation.get('started_at') or '—').replace('T', ' ')[:25]}\n"
            f"完成: {str(operation.get('completed_at') or '—').replace('T', ' ')[:25]}\n"
            f"发起: {OperationTableModel.INITIATOR_NAMES.get(str(operation.get('initiator') or ''), operation.get('initiator') or '—')}\n"
            f"环境: {operation.get('context') or 'production'} · 第 {operation.get('attempt_no') or 1} 次尝试"
        )
        self.operation_detail_metadata.setPlainText(metadata)
        events = detail.get("events")
        step_names = {
            "recovery_requested": "自动恢复请求",
            "manual_qmt_restart_requested": "人工重启请求",
            "recovery_result": "QMT 启动步骤返回",
            "manual_qmt_restart_result": "QMT 启动步骤返回",
            "recovery_verified": "稳定验证通过",
            "manual_qmt_restart_verified": "稳定验证通过",
            "recovery_verification_failed": "稳定验证失败",
            "manual_qmt_restart_verification_failed": "稳定验证失败",
            "manual_check_requested": "立即检测请求",
            "manual_check_result": "立即检测结果",
            "legacy_stable_verification": "历史稳定验证通过",
            "legacy_verification_inferred_failed": "历史验证结果回填",
        }
        lines: list[str] = []
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("event_type") or "")
                time_value = str(event.get("time") or "").replace("T", " ")[:19]
                event_summary = str(event.get("summary") or "")
                lines.append(
                    f"{time_value}  {step_names.get(event_type, event_type)}\n"
                    f"  {event_summary}"
                )
        self.operation_detail_steps.setPlainText(
            "\n\n".join(lines) or "暂无关联步骤"
        )
        self.operation_raw.setPlainText(
            json.dumps(detail or operation, ensure_ascii=False, indent=2, default=str)[
                :100_000
            ]
        )

    @Slot(int, object)
    def _apply_trends(self, generation: int, values: object) -> None:
        if generation != self._trend_generation:
            return
        samples = []
        for document in values if isinstance(values, list) else []:
            if isinstance(document, dict):
                sample = sample_from_document(document)
                if sample is not None:
                    samples.append(sample)
        by_time = {sample.at.isoformat(): sample for sample in samples}
        for sample in self._history:
            by_time[sample.at.isoformat()] = sample
        self._render_monitor_samples(sorted(by_time.values(), key=lambda item: item.at))

    def _render_monitor_samples(self, samples: list[HealthSample]) -> None:
        for chart in (self.health_timeline, self.latency_chart, self.task_chart):
            chart.set_samples(samples)
            chart.set_range(self._monitor_range)
        availability, average, p95, trade, incidents, coverage = compute_trend_metrics(
            samples, self._monitor_range
        )
        self.availability_metric.set_value(availability, coverage)
        self.latency_metric.set_value(p95, f"平均 {average} / P95 {p95}")
        selection_name = self.config.trade_system.selection_engine.title()
        self.trade_metric.set_value(
            trade, f"Fuel / {selection_name} 选股 / Rocket 下单"
        )
        self.incident_metric.set_value(incidents, coverage)

    @Slot(QModelIndex)
    def _show_event_index(self, index: QModelIndex) -> None:
        event = self.event_model.event_at(index.row())
        if event is None:
            return
        severity = str(event.get("severity") or "info")
        self.event_detail_title.setText(str(event.get("event_type") or "事件详情"))
        self.event_detail_severity.setText({"info": "信息", "warning": "警告", "critical": "严重"}.get(severity, severity))
        self.event_detail_severity.set_tone("danger" if severity == "critical" else "warning" if severity == "warning" else "info")
        summary = str(event.get("summary") or event.get("reason") or "暂无可读摘要")
        component = str(event.get("component_id") or "quant_guardian")
        timestamp = str(event.get("time") or "").replace("T", " ")[:19]
        self.event_detail_summary.setText(f"{timestamp} · {component}\n{summary}")
        evidence = event.get("evidence")
        if not isinstance(evidence, dict):
            evidence = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        evidence_text = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}"
            for key, value in evidence.items()
            if key not in {"account_id", "security_code", "price", "amount"}
        )
        self.event_detail_evidence.setPlainText(evidence_text or "没有额外结构化证据")
        raw = json.dumps(event, ensure_ascii=False, indent=2, default=str)
        self.event_raw.setPlainText(raw[:100_000])

    def _toggle_event_raw(self, visible: bool) -> None:
        self.event_raw.setVisible(visible)

    # ----- State, actions, settings ----------------------------------------

    @Slot(object)
    def apply_status(self, status: ServiceStatus) -> None:
        self._last_status = status
        components = status.components or {}
        if components.get("qmt_api") and components.get("trade_system"):
            sample = sample_from_status(status)
            if not self._history or self._history[-1].at != sample.at:
                self._history.append(sample)
        self.state_hero.update_status(status)
        self.qmt_card.update_component(components.get("qmt_api", {}))
        self.trade_card.update_component(components.get("trade_system", {}))
        if not self._operation_in_progress:
            restart_pending = status.state in {
                GuardianState.RECOVERING,
                GuardianState.VERIFYING,
            }
            self.qmt_restart_button.setEnabled(not restart_pending)
            self.qmt_restart_button.setToolTip(
                "当前QMT重启仍在执行或稳定验证中"
                if restart_pending
                else "确认后人工重启QMT；不受观察模式限制"
            )
        attention = status.attention or {}
        market_closed_healthy = (
            status.state is GuardianState.HEALTHY
            and not attention.get("required")
            and (status.schedule or {}).get("trading_day") is False
        )
        if market_closed_healthy:
            self.top_state_pill.setText("休市监控")
            self.top_state_pill.set_tone("neutral")
        elif status.state is GuardianState.HEALTHY and attention.get("required"):
            level = str(attention.get("level") or "warning")
            self.top_state_pill.setText("需要处理" if level == "critical" else "需要关注")
            self.top_state_pill.set_tone("danger" if level == "critical" else "warning")
        else:
            label = {
                GuardianState.STARTING: "启动验证",
                GuardianState.HEALTHY: "运行健康",
                GuardianState.SUSPECT: "正在复核",
                GuardianState.DEGRADED: "链路故障",
                GuardianState.RECOVERING: "恢复中",
                GuardianState.VERIFYING: "验证中",
                GuardianState.MANUAL_REQUIRED: "需要人工",
                GuardianState.LOCKOUT: "已锁定",
                GuardianState.PAUSED: "已暂停",
            }[status.state]
            tone = (
                "success"
                if status.state is GuardianState.HEALTHY
                else "warning"
                if status.state in {GuardianState.STARTING, GuardianState.SUSPECT, GuardianState.VERIFYING, GuardianState.PAUSED}
                else "info"
                if status.state is GuardianState.RECOVERING
                else "danger"
            )
            self.top_state_pill.setText(label)
            self.top_state_pill.set_tone(tone)
        if self.page_stack.currentIndex() == 1:
            self._render_monitor_samples(list(self._history))
        if self.tray:
            palette = DARK if self._dark else LIGHT
            color = palette[
                "idle"
                if market_closed_healthy
                else "green"
                if status.state is GuardianState.HEALTHY and not attention.get("required")
                else "amber"
                if status.state in {GuardianState.STARTING, GuardianState.SUSPECT, GuardianState.VERIFYING}
                else "red"
            ]
            self.tray.setIcon(_tray_icon(color))
            self.tray.setToolTip(f"Quant Guardian · {self.top_state_pill.text()}")

    def _run_hero_action(self) -> None:
        target = str(self.state_hero.action_button.property("target") or "")
        if target == "unlock":
            self._run_service_operation("unlock", self.service.unlock)
        elif target == "manual":
            self.confirm_manual()

    def _run_service_operation(self, name: str, operation) -> None:
        if self._operation_in_progress:
            return
        self._set_operation_busy(True)

        def worker() -> None:
            try:
                status = operation()
                self.bridge.operation_finished.emit(name, status, "")
            except Exception as exc:  # noqa: BLE001
                self.bridge.operation_finished.emit(name, None, str(exc))

        threading.Thread(target=worker, name=f"qg-ui-{name}", daemon=True).start()

    @Slot(str, object, str)
    def _operation_finished(self, name: str, status: object, error: str) -> None:
        self._set_operation_busy(False)
        if isinstance(status, ServiceStatus):
            self.apply_status(status)
        if error:
            QMessageBox.warning(self, "操作失败", error)
        elif name == "restart_qmt":
            QMessageBox.information(
                self,
                "QMT重启已执行",
                "QMT启动命令已经执行，Quant Guardian正在继续验证进程与XTQuant连接。",
            )
        elif name == "restart_trade":
            QMessageBox.information(
                self,
                "Quantclass重启完成",
                "Quantclass客户端已重新启动。Fuel、Zeus与Rocket进程未被Quant Guardian主动终止。",
            )
        elif name == "save":
            QMessageBox.information(self, "设置已保存", "配置已写入，新的监控频率会在下一轮生效。")
        if self.page_stack.currentIndex() == 1:
            self._request_trends()
            self._request_operations(reset=True)
            self._request_events(reset=True)

    def manual_restart(self) -> None:
        dialog = RestartConfirmDialog(self._last_status.safety_reason, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run_service_operation(
                "restart_qmt",
                lambda: self.service.manual_restart(operator_confirmed=True),
            )

    def manual_restart_trade_system(self) -> None:
        rocket_active = bool((self._last_status.rocket or {}).get("active"))
        dialog = QuantclassRestartConfirmDialog(
            self.config.trade_system.client_executable,
            rocket_active=rocket_active,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run_service_operation(
                "restart_trade",
                lambda: self.service.manual_restart_trade_system(
                    operator_confirmed=True
                ),
            )

    def confirm_manual(self) -> None:
        dialog = ManualConfirmDialog(self._last_status, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run_service_operation("manual", self.service.acknowledge_manual)

    def save_settings(self) -> None:
        try:
            self.config.mode = str(self.mode_combo.currentData())
            self.config.monitoring.allow_idle_recovery = self.allow_idle_recovery.isChecked()
            self.config.recovery.allow_qmt_restart_while_rocket_active = self.allow_qmt_with_rocket.isChecked()
            self.config.recovery.require_manual_rocket_resume = self.manual_rocket_resume.isChecked()
            self.config.recovery.max_attempts_per_30_minutes = self.max_30.value()
            self.config.recovery.max_attempts_per_day = self.max_day.value()
            self.config.recovery.graceful_close_seconds = self.graceful_close.value()
            backoff = [int(value.strip()) for value in self.backoff.text().split(",") if value.strip()]
            if not backoff:
                raise ValueError("退避秒数至少需要一个正整数")
            self.config.recovery.backoff_seconds = backoff
            self.config.qmt.launcher = self.qmt_launcher.text().strip()
            self.config.qmt.working_directory = self.qmt_workdir.text().strip()
            self.config.qmt.userdata_directory = self.qmt_userdata.text().strip()
            self.config.qmt.log_directory = self.qmt_logdir.text().strip()
            self.config.probe.python_executable = self.probe_python.text().strip()
            self.config.probe.xtquant_parent = self.xtquant_parent.text().strip()
            self.config.trade_system.enabled = self.trade_enabled.isChecked()
            self.config.trade_system.selection_engine = str(
                self.selection_engine.currentData()
            )
            self.trade_card.rows[
                "trade_system.selection"
            ].name_label.setText(
                f"选股内核 · {self.config.trade_system.selection_engine.title()}"
            )
            self.config.trade_system.data_root = self.trade_root.text().strip()
            self.config.trade_system.client_executable = (
                self.quantclass_executable.text().strip()
            )
            self.config.trade_system.quantclass_config = self.quantclass_config.text().strip()
            self.config.trade_system.fuel_status_file = self.fuel_status.text().strip()
            self.config.trade_system.aqua_log_file = self.aqua_log.text().strip()
            self.config.trade_system.zeus_log_file = self.zeus_log.text().strip()
            self.config.trade_system.rocket_log_directory = self.rocket_log.text().strip()
            self.config.monitoring.active_start = self.active_start.time().toString("HH:mm")
            self.config.monitoring.active_end = self.active_end.time().toString("HH:mm")
            self.config.monitoring.active_interval_seconds = self.active_interval.value()
            self.config.monitoring.idle_interval_seconds = float(self.idle_interval.value())
            self.config.monitoring.anomaly_retry_seconds = self.anomaly_retry.value()
            self.config.monitoring.anomaly_confirmation_checks = self.anomaly_checks.value()
            self.config.monitoring.business_summary_interval_seconds = self.business_interval.value()
            self.config.monitoring.business_summary_timeout_seconds = self.business_timeout.value()
            self.config.probe.timeout_seconds = self.health_timeout.value()
            self.config.thresholds.failure_threshold = self.failure_threshold.value()
            self.config.thresholds.failure_window_seconds = self.failure_window.value()
            self.config.thresholds.startup_grace_seconds = self.startup_grace.value()
            self.config.thresholds.verify_successes = self.verify_successes.value()
            self.config.thresholds.verify_min_span_seconds = self.verify_span.value()
            self.config.thresholds.verification_timeout_seconds = (
                self.verification_timeout.value()
            )
            self.config.trading.manual_closed_dates = [
                line.strip() for line in self.manual_closed.toPlainText().splitlines() if line.strip()
            ]
            self.config.trading.manual_open_dates = [
                line.strip() for line in self.manual_open.toPlainText().splitlines() if line.strip()
            ]
            self.config.notifications.desktop_enabled = self.desktop_notifications.isChecked()
            self.config.notifications.sound_on_critical = self.sound_critical.isChecked()
            self.config.notifications.dedupe_minutes = self.notification_dedupe.value()
            self.config.diagnostics.retention_days = self.retention_days.value()
            self.config.diagnostics.sqlite_index_enabled = self.sqlite_enabled.isChecked()
            self.config.monitoring.max_chart_points = self.max_chart_points.value()
            save_config(self.config, self.config_path)
            record_change = getattr(self.service, "record_settings_changed", None)
            if callable(record_change):
                record_change()
            self.bridge.operation_finished.emit("save", self._last_status, "")
            self.service.request_check()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法保存设置", str(exc))

    # ----- Theme, notifications, lifecycle ---------------------------------

    def toggle_theme(self) -> None:
        self._apply_theme(not self._dark)

    def _apply_theme(self, dark: bool) -> None:
        self._dark = dark
        QApplication.instance().setStyleSheet(build_stylesheet(dark=dark))
        self.theme_button.setIcon(line_icon("sun" if dark else "moon", 18))
        self.theme_button.setToolTip("切换浅色主题" if dark else "切换深色主题")
        self.state_hero.set_dark(dark)
        self.state_hero.update_status(self._last_status)
        for chart in (self.health_timeline, self.latency_chart, self.task_chart):
            chart.set_dark(dark)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(_tray_icon(LIGHT["indigo"]), self)
        menu = QMenu()
        show_action = QAction("打开 Quant Guardian", menu)
        show_action.triggered.connect(self.show_window)
        check_action = QAction("立即检测", menu)
        check_action.triggered.connect(self._run_check)
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show_action)
        menu.addAction(check_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    @Slot(object)
    def show_notification(self, notification: Notification) -> None:
        if self.tray and self.config.notifications.desktop_enabled:
            icon = QSystemTrayIcon.MessageIcon.Critical if notification.severity == "critical" else QSystemTrayIcon.MessageIcon.Warning if notification.severity == "warning" else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(notification.title, notification.message, icon, 7000)

    def export_diagnostics(self) -> None:
        destination = QFileDialog.getExistingDirectory(self, "选择诊断包保存目录")
        if not destination:
            return
        try:
            path = self.service.export_diagnostics(Path(destination))
            QMessageBox.information(self, "导出完成", f"诊断包已保存：\n{path}")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def open_onboarding(self) -> None:
        FirstRunDialog(self.config, self.config_path, self).exec()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._allow_close or self.tray is None:
            event.accept()
        else:
            event.ignore()
            self.hide()
            self.tray.showMessage("Quant Guardian", "监控仍在后台运行。", QSystemTrayIcon.MessageIcon.Information, 2500)

    def quit_application(self) -> None:
        self._allow_close = True
        self.service.stop()
        if self.tray:
            self.tray.hide()
        QApplication.instance().quit()
