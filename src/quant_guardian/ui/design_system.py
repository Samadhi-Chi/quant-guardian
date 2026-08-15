from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

from quant_guardian.domain.models import GuardianState

LIGHT = {
    "canvas": "#F3F5F9",
    "surface": "#FFFFFF",
    "surface_alt": "#F8F9FC",
    "surface_hover": "#F0F2F8",
    "border": "#DFE3EB",
    "border_strong": "#C8CEDA",
    "text": "#171A23",
    "text_muted": "#697184",
    "text_faint": "#9299A8",
    "indigo": "#5B5BD6",
    "indigo_hover": "#4E4EC4",
    "indigo_soft": "#EEEEFF",
    "green": "#168568",
    "green_soft": "#E8F6F1",
    "amber": "#BD7622",
    "amber_soft": "#FFF3E2",
    "red": "#C54C58",
    "red_soft": "#FDECEF",
    "blue": "#3E79C7",
    "blue_soft": "#EAF2FD",
    "idle": "#8290A8",
    "shadow": "#160F172A",
}

DARK = {
    "canvas": "#111318",
    "surface": "#191C23",
    "surface_alt": "#20242D",
    "surface_hover": "#272C37",
    "border": "#303641",
    "border_strong": "#454C59",
    "text": "#F0F2F6",
    "text_muted": "#A8AFBD",
    "text_faint": "#7D8595",
    "indigo": "#8B8BEA",
    "indigo_hover": "#9B9BF1",
    "indigo_soft": "#2A2947",
    "green": "#55B79D",
    "green_soft": "#18372F",
    "amber": "#E0A356",
    "amber_soft": "#3A2B18",
    "red": "#E47A84",
    "red_soft": "#422329",
    "blue": "#71A6E8",
    "blue_soft": "#1E304A",
    "idle": "#71809A",
    "shadow": "#52000000",
}


@dataclass(frozen=True, slots=True)
class StateDescriptor:
    title: str
    kicker: str
    accent: str
    soft: str
    icon: str


STATE_DESCRIPTORS = {
    GuardianState.STARTING: StateDescriptor(
        "正在建立健康基线", "启动验证", LIGHT["blue"], LIGHT["blue_soft"], "clock"
    ),
    GuardianState.HEALTHY: StateDescriptor(
        "交易链路运行健康", "全部关键检查通过", LIGHT["green"], LIGHT["green_soft"], "shield_check"
    ),
    GuardianState.SUSPECT: StateDescriptor(
        "检测到短暂异常", "正在复核，不会立即重启", LIGHT["amber"], LIGHT["amber_soft"], "activity"
    ),
    GuardianState.DEGRADED: StateDescriptor(
        "交易链路已确认故障", "连续证据达到恢复阈值", LIGHT["red"], LIGHT["red_soft"], "warning"
    ),
    GuardianState.RECOVERING: StateDescriptor(
        "正在执行受控恢复", "仅处理 QMT，不会自动恢复策略", LIGHT["blue"], LIGHT["blue_soft"], "repair"
    ),
    GuardianState.VERIFYING: StateDescriptor(
        "QMT 已启动，正在稳定性验证", "通过连续检查后才会恢复健康状态", LIGHT["blue"], LIGHT["blue_soft"], "pulse"
    ),
    GuardianState.MANUAL_REQUIRED: StateDescriptor(
        "需要人工核对实盘", "请确认委托、成交与持仓后再处理策略", LIGHT["red"], LIGHT["red_soft"], "hand"
    ),
    GuardianState.LOCKOUT: StateDescriptor(
        "自动恢复已安全锁定", "重试次数达到上限，等待人工判断", LIGHT["red"], LIGHT["red_soft"], "lock"
    ),
    GuardianState.PAUSED: StateDescriptor(
        "自动恢复已暂停", "监控与记录仍在继续", LIGHT["amber"], LIGHT["amber_soft"], "pause"
    ),
}


STATE_COLORS = {
    GuardianState.STARTING: LIGHT["blue"],
    GuardianState.HEALTHY: LIGHT["green"],
    GuardianState.SUSPECT: LIGHT["amber"],
    GuardianState.DEGRADED: LIGHT["red"],
    GuardianState.RECOVERING: LIGHT["blue"],
    GuardianState.VERIFYING: LIGHT["blue"],
    GuardianState.MANUAL_REQUIRED: LIGHT["red"],
    GuardianState.LOCKOUT: LIGHT["red"],
    GuardianState.PAUSED: LIGHT["amber"],
}


_UI_FONT_FAMILY: str | None = None


def install_ui_font() -> str:
    """Register the Windows Chinese UI font explicitly for Qt offscreen and packaged runs."""

    global _UI_FONT_FAMILY
    if _UI_FONT_FAMILY:
        return _UI_FONT_FAMILY
    for font_path in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/Deng.ttf"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ):
        if not font_path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            _UI_FONT_FAMILY = next((family for family in families if "UI" in family), families[0])
            return _UI_FONT_FAMILY
    _UI_FONT_FAMILY = "Microsoft YaHei UI"
    return _UI_FONT_FAMILY

def _pen(color: str | QColor, width: float = 1.8) -> QPen:
    value = QColor(color) if isinstance(color, str) else color
    pen = QPen(value, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    return pen


def paint_line_icon(painter: QPainter, name: str, rect: QRectF, color: str | QColor) -> None:
    """Paint the small, dependency-free line icon set used by the desktop UI."""

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = min(rect.width(), rect.height()) / 24.0
    painter.translate(rect.left() + (rect.width() - 24 * scale) / 2, rect.top() + (rect.height() - 24 * scale) / 2)
    painter.scale(scale, scale)
    painter.setPen(_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name in {"shield", "shield_check"}:
        path = QPainterPath(QPointF(12, 2.5))
        path.lineTo(20, 5.5)
        path.lineTo(19, 13.2)
        path.cubicTo(18.4, 17.1, 15.8, 20, 12, 21.5)
        path.cubicTo(8.2, 20, 5.6, 17.1, 5, 13.2)
        path.lineTo(4, 5.5)
        path.closeSubpath()
        painter.drawPath(path)
        if name == "shield_check":
            painter.drawPolyline(QPolygonF([QPointF(8.2, 12), QPointF(10.7, 14.5), QPointF(15.9, 9.2)]))
    elif name == "overview":
        for box in (QRectF(3, 3, 7, 7), QRectF(14, 3, 7, 7), QRectF(3, 14, 7, 7), QRectF(14, 14, 7, 7)):
            painter.drawRoundedRect(box, 1.4, 1.4)
    elif name == "trend":
        painter.drawLine(QPointF(3, 20), QPointF(21, 20))
        painter.drawLine(QPointF(4, 19), QPointF(4, 5))
        painter.drawPolyline(QPolygonF([QPointF(6, 15), QPointF(10, 11), QPointF(13, 13), QPointF(19, 6)]))
        painter.drawPolyline(QPolygonF([QPointF(15.7, 6), QPointF(19, 6), QPointF(19, 9.3)]))
    elif name == "events":
        for y in (6, 12, 18):
            painter.drawEllipse(QPointF(4.5, y), 1.2, 1.2)
            painter.drawLine(QPointF(8, y), QPointF(20, y))
    elif name == "settings":
        painter.drawEllipse(QPointF(12, 12), 3.1, 3.1)
        painter.drawEllipse(QPointF(12, 12), 7.1, 7.1)
        for a, b in ((12, 2.5), (12, 5), (12, 19), (12, 21.5), (2.5, 12), (5, 12), (19, 12), (21.5, 12)):
            if a == 12:
                painter.drawLine(QPointF(a, b), QPointF(a, 7 if b < 12 else 17))
            else:
                painter.drawLine(QPointF(a, b), QPointF(7 if a < 12 else 17, b))
    elif name == "refresh":
        painter.drawArc(QRectF(4, 4, 16, 16), 36 * 16, 278 * 16)
        painter.drawPolyline(QPolygonF([QPointF(18.3, 4.5), QPointF(20, 8.2), QPointF(16.2, 8.1)]))
    elif name == "pause":
        painter.drawRoundedRect(QRectF(7, 5, 3.2, 14), 1, 1)
        painter.drawRoundedRect(QRectF(13.8, 5, 3.2, 14), 1, 1)
    elif name == "play":
        path = QPainterPath(QPointF(8, 5))
        path.lineTo(19, 12)
        path.lineTo(8, 19)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "export":
        painter.drawRoundedRect(QRectF(4, 10, 16, 10), 2, 2)
        painter.drawLine(QPointF(12, 4), QPointF(12, 14))
        painter.drawPolyline(QPolygonF([QPointF(8.5, 7.5), QPointF(12, 4), QPointF(15.5, 7.5)]))
    elif name in {"lock", "unlock"}:
        painter.drawRoundedRect(QRectF(5, 10, 14, 11), 2, 2)
        if name == "lock":
            painter.drawArc(QRectF(7.5, 3, 9, 12), 0, 180 * 16)
        else:
            painter.drawArc(QRectF(11, 3, 8, 12), 0, 150 * 16)
        painter.drawLine(QPointF(12, 14), QPointF(12, 17.3))
    elif name == "check":
        painter.drawPolyline(QPolygonF([QPointF(4.5, 12.5), QPointF(9.3, 17.2), QPointF(19.5, 6.8)]))
    elif name == "warning":
        painter.drawPolygon(QPolygonF([QPointF(12, 3), QPointF(21, 20), QPointF(3, 20)]))
        painter.drawLine(QPointF(12, 9), QPointF(12, 14))
        painter.drawPoint(QPointF(12, 17.3))
    elif name == "terminal":
        painter.drawRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
        painter.drawPolyline(QPolygonF([QPointF(7, 9), QPointF(10, 12), QPointF(7, 15)]))
        painter.drawLine(QPointF(12.5, 15), QPointF(17, 15))
    elif name in {"activity", "pulse"}:
        painter.drawPolyline(QPolygonF([QPointF(2.5, 13), QPointF(6.5, 13), QPointF(9, 7), QPointF(13, 18), QPointF(16, 11), QPointF(21.5, 11)]))
    elif name == "rocket":
        path = QPainterPath(QPointF(8, 16))
        path.cubicTo(8.5, 8, 12.5, 4.2, 18.5, 3)
        path.cubicTo(19.2, 9.2, 15.5, 14.3, 8, 16)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawEllipse(QPointF(14.6, 7.2), 1.6, 1.6)
        painter.drawPolyline(QPolygonF([QPointF(8.5, 12.2), QPointF(4, 12.5), QPointF(6.5, 17)]))
        painter.drawPolyline(QPolygonF([QPointF(11.5, 15.2), QPointF(11.5, 20), QPointF(16, 17.2)]))
        painter.drawLine(QPointF(6.5, 18), QPointF(3.7, 20.3))
    elif name == "clock":
        painter.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        painter.drawLine(QPointF(12, 7), QPointF(12, 12))
        painter.drawLine(QPointF(12, 12), QPointF(16, 14))
    elif name == "info":
        painter.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        painter.drawLine(QPointF(12, 10.5), QPointF(12, 17))
        painter.drawPoint(QPointF(12, 7))
    elif name == "repair":
        painter.drawLine(QPointF(5, 19), QPointF(14, 10))
        painter.drawLine(QPointF(8, 20), QPointF(17, 11))
        painter.drawArc(QRectF(12, 3, 9, 9), 115 * 16, 235 * 16)
        painter.drawLine(QPointF(4.5, 18.5), QPointF(8.5, 20.5))
    elif name == "hand":
        path = QPainterPath(QPointF(7, 12))
        path.lineTo(7, 8)
        path.cubicTo(7, 6.5, 9, 6.5, 9, 8)
        path.lineTo(9, 5.5)
        path.cubicTo(9, 4, 11, 4, 11, 5.5)
        path.lineTo(11, 4.5)
        path.cubicTo(11, 3, 13, 3, 13, 4.5)
        path.lineTo(13, 5.3)
        path.cubicTo(13, 3.8, 15, 3.8, 15, 5.3)
        path.lineTo(15, 11)
        path.lineTo(17, 9)
        path.cubicTo(19, 7.5, 20.5, 9.5, 19, 11.5)
        path.lineTo(15, 18.5)
        path.lineTo(9, 18.5)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "database":
        painter.drawEllipse(QRectF(4, 3, 16, 6))
        painter.drawArc(QRectF(4, 9, 16, 6), 180 * 16, 180 * 16)
        painter.drawArc(QRectF(4, 15, 16, 6), 180 * 16, 180 * 16)
        painter.drawLine(QPointF(4, 6), QPointF(4, 18))
        painter.drawLine(QPointF(20, 6), QPointF(20, 18))
    elif name == "bell":
        painter.drawArc(QRectF(5, 4, 14, 14), 0, 180 * 16)
        painter.drawLine(QPointF(5, 11), QPointF(4, 17))
        painter.drawLine(QPointF(19, 11), QPointF(20, 17))
        painter.drawLine(QPointF(4, 17), QPointF(20, 17))
        painter.drawArc(QRectF(9, 16, 6, 5), 180 * 16, 180 * 16)
    elif name == "folder":
        path = QPainterPath(QPointF(3, 7))
        path.lineTo(9, 7)
        path.lineTo(11, 9)
        path.lineTo(21, 9)
        path.lineTo(19, 19)
        path.lineTo(4, 19)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "network":
        painter.drawArc(QRectF(3, 4, 18, 15), 35 * 16, 110 * 16)
        painter.drawArc(QRectF(7, 9, 10, 8), 35 * 16, 110 * 16)
        painter.drawPoint(QPointF(12, 19))
    elif name == "moon":
        path = QPainterPath()
        path.addEllipse(QRectF(4, 3, 16, 18))
        cutout = QPainterPath()
        cutout.addEllipse(QRectF(9, 1, 14, 15))
        painter.drawPath(path.subtracted(cutout))
    elif name == "sun":
        painter.drawEllipse(QPointF(12, 12), 4, 4)
        for x1, y1, x2, y2 in ((12, 2, 12, 5), (12, 19, 12, 22), (2, 12, 5, 12), (19, 12, 22, 12), (5, 5, 7, 7), (17, 17, 19, 19), (19, 5, 17, 7), (7, 17, 5, 19)):
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    elif name == "chevron_right":
        painter.drawPolyline(QPolygonF([QPointF(9, 5), QPointF(16, 12), QPointF(9, 19)]))
    elif name == "close":
        painter.drawLine(QPointF(5, 5), QPointF(19, 19))
        painter.drawLine(QPointF(19, 5), QPointF(5, 19))
    else:
        painter.drawEllipse(QPointF(12, 12), 8, 8)

    painter.restore()


def icon_pixmap(name: str, color: str, size: int = 20) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_line_icon(painter, name, QRectF(1, 1, size - 2, size - 2), color)
    painter.end()
    return pixmap


def line_icon(name: str, size: int = 20) -> QIcon:
    icon = QIcon()
    icon.addPixmap(icon_pixmap(name, LIGHT["text_muted"], size), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(icon_pixmap(name, LIGHT["indigo"], size), QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(icon_pixmap(name, LIGHT["text_faint"], size), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


def build_stylesheet(*, dark: bool = False) -> str:
    c = DARK if dark else LIGHT
    font_family = _UI_FONT_FAMILY or "Microsoft YaHei UI"
    return f"""
    QMainWindow, QDialog, QWidget#appRoot {{
        background: {c['canvas']};
        color: {c['text']};
    }}
    QWidget {{
        color: {c['text']};
        font-family: "{font_family}";
        font-size: 13px;
    }}
    QFrame#topBar {{
        background: {c['surface']};
        border-bottom: 1px solid {c['border']};
    }}
    QLabel#brandTitle {{ font-size: 15px; font-weight: 650; }}
    QLabel#brandCaption, QLabel[role="muted"] {{ color: {c['text_muted']}; }}
    QLabel#pageTitle {{ font-size: 22px; font-weight: 650; }}
    QLabel#pageSubtitle {{ color: {c['text_muted']}; }}
    QLabel#sectionTitle {{ font-size: 15px; font-weight: 650; }}
    QLabel#sectionCaption {{ color: {c['text_muted']}; font-size: 12px; }}
    QLabel#heroTitle {{ font-size: 20px; font-weight: 680; }}
    QLabel#heroKicker {{ font-size: 11px; font-weight: 650; letter-spacing: 0.6px; }}
    QLabel#heroReason {{ color: {c['text_muted']}; font-size: 13px; }}
    QLabel#metricValue {{ font-size: 23px; font-weight: 680; }}
    QLabel#metricLabel {{ color: {c['text_muted']}; font-size: 12px; }}
    QLabel#cardTitle {{ font-size: 14px; font-weight: 650; }}
    QLabel#cardCaption {{ color: {c['text_muted']}; font-size: 12px; }}
    QLabel#detailTitle {{ font-size: 16px; font-weight: 650; }}
    QLabel#dialogTitle {{ font-size: 19px; font-weight: 680; }}
    QFrame#card, QFrame#metricCard, QFrame#stateDetail, QFrame#safetyStrip {{
        background: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 10px;
    }}
    QFrame#servicePanel {{
        background: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 8px;
    }}
    QWidget#servicePanelHeader {{
        background: {c['surface_alt']};
        border: 0;
        border-bottom: 1px solid {c['border']};
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
    }}
    QFrame#heroBanner {{
        background: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 8px;
    }}
    QFrame#serviceRow {{
        background: transparent;
        border-bottom: 1px solid {c['border']};
    }}
    QFrame#serviceRowLast {{ background: transparent; border: 0; }}
    QLabel[pill="true"] {{
        border-radius: 10px;
        padding: 3px 9px;
        font-size: 11px;
        font-weight: 650;
    }}
    QLabel[tone="success"] {{ color: {c['green']}; background: {c['green_soft']}; }}
    QLabel[tone="warning"] {{ color: {c['amber']}; background: {c['amber_soft']}; }}
    QLabel[tone="danger"] {{ color: {c['red']}; background: {c['red_soft']}; }}
    QLabel[tone="info"] {{ color: {c['blue']}; background: {c['blue_soft']}; }}
    QLabel[tone="neutral"] {{ color: {c['text_muted']}; background: {c['surface_alt']}; }}
    QToolButton#navButton {{
        border: 0;
        border-radius: 4px;
        background: transparent;
        color: {c['text_muted']};
        padding: 7px 12px;
        font-weight: 550;
    }}
    QToolButton#navButton:hover {{ background: {c['surface_hover']}; color: {c['text']}; }}
    QToolButton#navButton:checked {{ background: {c['indigo_soft']}; color: {c['indigo']}; }}
    QToolButton#iconButton {{
        border: 1px solid {c['border']};
        border-radius: 4px;
        background: {c['surface']};
        padding: 5px;
    }}
    QToolButton#iconButton:hover {{ background: {c['surface_hover']}; }}
    QToolButton#primaryToolButton {{
        min-height: 34px;
        padding: 0 13px;
        border: 1px solid {c['indigo']};
        border-radius: 4px;
        background: {c['indigo']};
        color: white;
        font-weight: 650;
    }}
    QToolButton#primaryToolButton:hover {{ background: {c['indigo_hover']}; border-color: {c['indigo_hover']}; }}
    QPushButton {{
        min-height: 34px;
        padding: 0 13px;
        border: 1px solid {c['border_strong']};
        border-radius: 4px;
        background: {c['surface']};
        color: {c['text']};
        font-weight: 550;
    }}
    QPushButton:hover {{ background: {c['surface_hover']}; border-color: {c['text_faint']}; }}
    QPushButton:pressed {{ background: {c['border']}; }}
    QPushButton:disabled {{ color: {c['text_faint']}; background: {c['surface_alt']}; border-color: {c['border']}; }}
    QPushButton[variant="primary"] {{ color: white; background: {c['indigo']}; border-color: {c['indigo']}; }}
    QPushButton[variant="primary"]:hover {{ background: {c['indigo_hover']}; border-color: {c['indigo_hover']}; }}
    QPushButton[variant="danger"] {{ color: white; background: {c['red']}; border-color: {c['red']}; }}
    QPushButton[variant="softDanger"] {{ color: {c['red']}; background: {c['red_soft']}; border-color: {c['red_soft']}; }}
    QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; color: {c['text_muted']}; }}
    QPushButton[segment="true"] {{ min-height: 28px; padding: 0 10px; border-radius: 6px; }}
    QPushButton[segment="true"]:checked {{ color: {c['indigo']}; background: {c['indigo_soft']}; border-color: {c['indigo_soft']}; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit, QComboBox, QTextEdit, QPlainTextEdit {{
        min-height: 34px;
        padding: 0 9px;
        background: {c['surface']};
        border: 1px solid {c['border_strong']};
        border-radius: 7px;
        selection-background-color: {c['indigo']};
    }}
    QTextEdit, QPlainTextEdit {{ padding: 8px; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
        border: 1px solid {c['indigo']};
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
    QTimeEdit::up-button, QTimeEdit::down-button {{ width: 0; border: 0; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid {c['border_strong']}; border-radius: 4px; background: {c['surface']}; }}
    QCheckBox::indicator:checked {{ background: {c['indigo']}; border-color: {c['indigo']}; }}
    QScrollArea, QAbstractScrollArea {{ background: {c['canvas']}; border: 0; }}
    QWidget#scrollBody, QWidget#scrollViewport {{ background: {c['canvas']}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 4px; min-height: 28px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QTableWidget, QTableView {{
        background: {c['surface']};
        alternate-background-color: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        gridline-color: transparent;
        outline: none;
    }}
    QTableWidget::item, QTableView::item {{ padding: 8px 9px; border-bottom: 1px solid {c['border']}; }}
    QTableWidget::item:selected, QTableView::item:selected {{ background: {c['indigo_soft']}; color: {c['text']}; }}
    QHeaderView::section {{
        background: {c['surface_alt']};
        color: {c['text_muted']};
        border: 0;
        border-bottom: 1px solid {c['border']};
        padding: 8px 9px;
        font-size: 11px;
        font-weight: 650;
    }}
    QSplitter::handle {{ background: transparent; width: 8px; }}
    QFrame#settingsNav {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QToolButton#settingsNavButton {{
        border: 0; border-radius: 7px; background: transparent; color: {c['text_muted']};
        padding: 9px 11px; text-align: left;
    }}
    QToolButton#settingsNavButton:hover {{ background: {c['surface_hover']}; color: {c['text']}; }}
    QToolButton#settingsNavButton:checked {{ background: {c['indigo_soft']}; color: {c['indigo']}; font-weight: 650; }}
    QFrame#formSection {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QGroupBox {{ border: 0; margin-top: 9px; font-weight: 650; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 0; padding: 0; }}
    QFrame#divider {{ background: {c['border']}; min-height: 1px; max-height: 1px; border: 0; }}
    QToolTip {{ color: {c['text']}; background: {c['surface']}; border: 1px solid {c['border_strong']}; padding: 5px; }}
    """


def set_dynamic_property(widget, name: str, value: str) -> None:
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def nav_icon_size() -> QSize:
    return QSize(18, 18)
