from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from quant_guardian.gateway.store import GatewayStore
from quant_guardian.ui.gateway_dialogs import TelegramSetupDialog, WeixinQrDialog


class OnePoll:
    def wait(self, _seconds: float) -> bool:
        return False

    def set(self) -> None:
        return None

    def clear(self) -> None:
        return None


class GatewayUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self.temporary.name) / "gateway.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_telegram_setup_dialog_renders_success_error_and_missing_input(self) -> None:
        dialog = TelegramSetupDialog(has_saved_token=False, store=self.store)
        self.addCleanup(dialog.deleteLater)
        dialog.resize(620, 360)
        dialog.show()
        self.application.processEvents()
        self.assertFalse(dialog.grab().isNull())
        self.assertFalse(dialog.save_button.isEnabled())

        dialog._apply_test({"username": "guardian_demo"}, "")
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertIn("@guardian_demo", dialog.status.text())
        dialog._apply_test({}, "AuthenticationError: rejected")
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertIn("连接失败", dialog.status.text())

        dialog.token.clear()
        with patch.object(QMessageBox, "warning") as warning:
            dialog._test()
        warning.assert_called_once()
        dialog.token.setText("  demo-token  ")
        self.assertEqual(dialog.token_value, "demo-token")

    def make_weixin_dialog(self) -> WeixinQrDialog:
        with patch("quant_guardian.ui.gateway_dialogs.QTimer") as timer:
            dialog = WeixinQrDialog()
        timer.singleShot.assert_called_once()
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_weixin_dialog_renders_qr_and_all_login_states(self) -> None:
        dialog = self.make_weixin_dialog()
        pixmap = dialog._qr_pixmap("https://example.invalid/synthetic-qr", target=160)
        self.assertFalse(pixmap.isNull())
        self.assertLessEqual(pixmap.width(), 160)

        dialog._apply_qr({}, "ChannelError: unavailable")
        self.assertIn("二维码获取失败", dialog.qr_label.text())
        with patch("quant_guardian.ui.gateway_dialogs.threading.Thread.start"):
            dialog._apply_qr(
                {
                    "qrcode": "synthetic",
                    "content": "https://example.invalid/synthetic-qr",
                    "base_url": "https://ilinkai.weixin.qq.com",
                },
                "",
            )
        self.assertFalse(dialog.qr_label.pixmap().isNull())
        self.assertIn("扫码", dialog.status.text())

        dialog._apply_poll({}, "temporary")
        self.assertIn("重试", dialog.status.text())
        dialog._apply_poll({"status": "scaned"}, "")
        self.assertIn("确认", dialog.status.text())
        dialog._apply_poll({"status": "scaned_but_redirect"}, "")
        self.assertIn("切换", dialog.status.text())
        dialog._apply_poll({"status": "expired"}, "")
        self.assertIn("过期", dialog.status.text())
        dialog._apply_poll({"status": "confirmed"}, "")
        self.assertIn("不完整", dialog.status.text())
        dialog._apply_poll(
            {
                "status": "confirmed",
                "account_id": "demo@im.bot",
                "token": "protected-by-caller",
                "base_url": "https://ilinkai.weixin.qq.com",
            },
            "",
        )
        self.assertTrue(dialog.finish_button.isEnabled())
        self.assertEqual(dialog.credentials["account_id"], "demo@im.bot")

    def test_weixin_poll_accepts_only_trusted_redirect_and_finishes(self) -> None:
        dialog = self.make_weixin_dialog()
        dialog._qr = {
            "qrcode": "synthetic",
            "base_url": "https://ilinkai.weixin.qq.com",
        }
        dialog._cancel = OnePoll()
        with patch(
            "quant_guardian.ui.gateway_dialogs.poll_qr_code",
            side_effect=[
                {
                    "status": "scaned_but_redirect",
                    "redirect_host": "attacker.invalid",
                },
                {
                    "status": "confirmed",
                    "account_id": "demo@im.bot",
                    "token": "token",
                },
            ],
        ) as poll:
            dialog._poll()
        self.assertEqual(
            poll.call_args_list[1].kwargs["base_url"],
            "https://ilinkai.weixin.qq.com",
        )

    def test_weixin_start_and_reject_are_non_blocking(self) -> None:
        dialog = self.make_weixin_dialog()
        with patch("quant_guardian.ui.gateway_dialogs.threading.Thread.start") as start:
            dialog._start()
        start.assert_called_once()
        dialog.reject()
        self.assertEqual(dialog.result(), dialog.DialogCode.Rejected)


if __name__ == "__main__":
    unittest.main()
