from __future__ import annotations

import threading
import urllib.parse

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from quant_guardian.gateway.channels.telegram import TelegramAdapter
from quant_guardian.gateway.channels.weixin import poll_qr_code, request_qr_code
from quant_guardian.gateway.config import TelegramGatewayConfig, is_trusted_weixin_base_url
from quant_guardian.gateway.store import GatewayStore
from quant_guardian.ui.design_system import LIGHT, icon_pixmap


def _header(icon_name: str, title: str, subtitle: str) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    icon = QLabel()
    icon.setFixedSize(42, 42)
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setPixmap(icon_pixmap(icon_name, LIGHT["indigo"], 24))
    icon.setStyleSheet(f"background:{LIGHT['indigo_soft']};border-radius:10px;")
    copy = QVBoxLayout()
    copy.setSpacing(3)
    heading = QLabel(title)
    heading.setObjectName("dialogTitle")
    caption = QLabel(subtitle)
    caption.setObjectName("cardCaption")
    caption.setWordWrap(True)
    copy.addWidget(heading)
    copy.addWidget(caption)
    layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
    layout.addLayout(copy, 1)
    return widget


class TelegramSetupDialog(QDialog):
    tested = Signal(object, str)

    def __init__(
        self,
        *,
        has_saved_token: bool,
        store: GatewayStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.identity: dict[str, str] = {}
        self.setWindowTitle("配置 Telegram")
        self.setMinimumWidth(590)
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 20)
        root.setSpacing(14)
        root.addWidget(
            _header(
                "notification",
                "连接 Telegram Bot",
                "Token 仅以当前 Windows 用户的 DPAPI 凭据保存，不会写入普通配置、日志或诊断包。",
            )
        )
        instructions = QLabel(
            "1. 在 Telegram 中打开 @BotFather，创建 Bot 并复制 Token。\n"
            "2. 粘贴后先测试连接。保存后再生成一次性配对码，将当前私聊设为唯一授权会话。"
        )
        instructions.setObjectName("cardCaption")
        instructions.setWordWrap(True)
        root.addWidget(instructions)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText(
            "已保存 Token；留空保持不变" if has_saved_token else "123456789:AA..."
        )
        self.token.setClearButtonEnabled(True)
        root.addWidget(self.token)
        self.status = QLabel("尚未测试")
        self.status.setObjectName("cardCaption")
        root.addWidget(self.status)
        actions = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test)
        actions.addWidget(self.test_button)
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("保存")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setEnabled(has_saved_token)
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(self.save_button)
        root.addLayout(actions)
        self.tested.connect(self._apply_test)

    @property
    def token_value(self) -> str:
        return self.token.text().strip()

    def _test(self) -> None:
        token = self.token_value
        if not token:
            QMessageBox.warning(self, "需要 Token", "请输入新的 Telegram Bot Token 后再测试。")
            return
        self.test_button.setEnabled(False)
        self.status.setText("正在连接 Telegram…")

        def worker() -> None:
            try:
                identity = TelegramAdapter(
                    TelegramGatewayConfig(), token=token, store=self.store
                ).test_connection()
                self.tested.emit(identity, "")
            except Exception as exc:  # network result is shown, never logged with token
                self.tested.emit({}, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, name="qg-ui-telegram-test", daemon=True).start()

    def _apply_test(self, identity: object, error: str) -> None:
        self.test_button.setEnabled(True)
        if error:
            self.status.setText("连接失败：" + error)
            self.save_button.setEnabled(False)
            return
        self.identity = dict(identity) if isinstance(identity, dict) else {}
        name = self.identity.get("username") or self.identity.get("name") or self.identity.get("id")
        self.status.setText(f"连接成功：{('@' + name) if self.identity.get('username') else name}")
        self.save_button.setEnabled(True)


class WeixinQrDialog(QDialog):
    qr_ready = Signal(object, str)
    qr_polled = Signal(object, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.credentials: dict[str, str] = {}
        self._cancel = threading.Event()
        self._qr: dict[str, str] = {}
        self.setWindowTitle("连接个人微信")
        self.setMinimumSize(610, 670)
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 20)
        root.setSpacing(13)
        root.addWidget(
            _header(
                "account",
                "微信扫码连接 iLink Bot",
                "仅启用个人私聊文本能力；群聊、媒体、文件、语音、Agent 和 Shell 均未实现。",
            )
        )
        self.qr_label = QLabel("正在获取二维码…")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumSize(360, 360)
        self.qr_label.setStyleSheet(
            f"background:{LIGHT['surface']};border:1px solid {LIGHT['border']};border-radius:12px;"
        )
        root.addWidget(self.qr_label, 1)
        self.status = QLabel("请稍候")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setObjectName("cardCaption")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        note = QLabel(
            "扫码后还需要在微信中确认。连接成功后，Quant Guardian 会再生成一个 5 分钟配对码；"
            "只有发送该配对码的首个私聊会被授权。"
        )
        note.setObjectName("cardCaption")
        note.setWordWrap(True)
        root.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.finish_button = QPushButton("完成")
        self.finish_button.setProperty("variant", "primary")
        self.finish_button.setEnabled(False)
        self.finish_button.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.finish_button)
        root.addLayout(buttons)
        self.qr_ready.connect(self._apply_qr)
        self.qr_polled.connect(self._apply_poll)
        QTimer.singleShot(0, self._start)

    def reject(self) -> None:
        self._cancel.set()
        super().reject()

    @staticmethod
    def _qr_pixmap(content: str, target: int = 340) -> QPixmap:
        import qrcode

        code = qrcode.QRCode(version=None, box_size=1, border=3)
        code.add_data(content)
        code.make(fit=True)
        matrix = code.get_matrix()
        size = len(matrix)
        image = QImage(size, size, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    image.setPixelColor(x, y, QColor("black"))
        return QPixmap.fromImage(image).scaled(
            target,
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

    def _start(self) -> None:
        self._cancel.clear()

        def worker() -> None:
            try:
                self.qr_ready.emit(request_qr_code(), "")
            except Exception as exc:
                self.qr_ready.emit({}, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, name="qg-ui-weixin-qr", daemon=True).start()

    def _apply_qr(self, value: object, error: str) -> None:
        if error:
            self.qr_label.setText("二维码获取失败")
            self.status.setText(error)
            return
        self._qr = dict(value) if isinstance(value, dict) else {}
        try:
            self.qr_label.setPixmap(self._qr_pixmap(self._qr["content"]))
        except Exception as exc:
            self.qr_label.setText(self._qr.get("content", ""))
            self.status.setText(f"二维码渲染失败：{type(exc).__name__}；可复制上方链接。")
        else:
            self.status.setText("请使用个人微信扫码，并在手机上确认")
        threading.Thread(target=self._poll, name="qg-ui-weixin-poll", daemon=True).start()

    def _poll(self) -> None:
        base_url = self._qr.get("base_url", "https://ilinkai.weixin.qq.com")
        while not self._cancel.wait(1):
            try:
                result = poll_qr_code(self._qr["qrcode"], base_url=base_url)
            except Exception as exc:
                self.qr_polled.emit({}, f"{type(exc).__name__}: {exc}")
                continue
            if result.get("status") == "scaned_but_redirect" and result.get("redirect_host"):
                redirect = str(result["redirect_host"]).strip()
                parsed = urllib.parse.urlsplit(
                    redirect if "://" in redirect else "https://" + redirect
                )
                if is_trusted_weixin_base_url(
                    urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
                ):
                    base_url = urllib.parse.urlunsplit(
                        ("https", parsed.netloc, "", "", "")
                    )
            self.qr_polled.emit(result, "")
            if result.get("status") in {"confirmed", "expired"}:
                return

    def _apply_poll(self, value: object, error: str) -> None:
        if error:
            self.status.setText("连接重试中：" + error)
            return
        result = dict(value) if isinstance(value, dict) else {}
        status = str(result.get("status") or "wait")
        if status == "scaned":
            self.status.setText("已扫码，请在微信中确认")
        elif status == "scaned_but_redirect":
            self.status.setText("已扫码，正在切换登录节点…")
        elif status == "expired":
            self.status.setText("二维码已过期，请关闭后重新打开")
        elif status == "confirmed":
            if not result.get("account_id") or not result.get("token"):
                self.status.setText("微信已确认，但返回的登录凭据不完整")
                return
            self.credentials = {str(key): str(item) for key, item in result.items()}
            self.status.setText("个人微信连接成功。点击“完成”后继续私聊配对。")
            self.finish_button.setEnabled(True)
            self._cancel.set()
