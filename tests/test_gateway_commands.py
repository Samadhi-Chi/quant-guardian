from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quant_guardian.gateway.commands import CommandProcessor, parse_command
from quant_guardian.gateway.config import (
    MessagingConfig,
    load_messaging_config,
    save_messaging_config,
)
from quant_guardian.gateway.ipc import GatewayIpcError
from quant_guardian.gateway.models import InboundMessage
from quant_guardian.gateway.store import GatewayStore


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def request(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        if action in {"status", "check"}:
            return {
                "status": {
                    "state": "healthy",
                    "components": {
                        "qmt_api": {"state": "healthy"},
                        "trade_system": {"state": "healthy"},
                    },
                    "attention": {"message": "无需操作"},
                    "schedule": {},
                }
            }
        if action == "incidents":
            return {"incidents": []}
        if action == "operations":
            return {"operations": []}
        if action == "confirm_restart_qmt":
            return {
                "reason": "QMT受控重启已启动",
                "operation_id": "QGO-1",
            }
        raise AssertionError(action)


class GatewayCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.path = root / "messaging.json"
        self.store = GatewayStore(root / "gateway.db")
        config = MessagingConfig(gateway_enabled=True)
        config.telegram.enabled = True
        config.telegram.allowed_user_ids = ["42"]
        config.telegram.home_chat_id = "42"
        config.weixin.enabled = True
        config.weixin.allowed_user_ids = ["wx-owner"]
        config.weixin.home_chat_id = "wx-owner"
        config.remote_control.enabled = True
        save_messaging_config(config, self.path)
        self.client = FakeClient()
        self.processor = CommandProcessor(
            store=self.store,
            client=self.client,
            config_path=self.path,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def message(self, text: str, *, channel: str = "telegram", sender: str = "42"):
        return InboundMessage(
            channel=channel,
            message_key=f"{channel}:{text}",
            sender_id=sender,
            chat_id=sender,
            text=text,
        )

    def test_parser_has_fixed_vocabulary_and_blocks_dangerous_commands(self) -> None:
        self.assertEqual(parse_command("").name, "help")
        self.assertEqual(parse_command("状态").name, "status")
        self.assertEqual(parse_command("重启 QMT").name, "restart_qmt")
        self.assertEqual(parse_command("重启 Quantclass").name, "forbidden_quantclass")
        self.assertEqual(parse_command("帮我下单").name, "forbidden")
        self.assertEqual(parse_command("powershell whoami").name, "forbidden")
        self.assertEqual(parse_command("随便聊聊").name, "unknown")

    def test_status_is_read_only_and_group_is_rejected(self) -> None:
        reply = self.processor.process(self.message("状态"))
        self.assertIn("QMT API：健康", reply.text)
        self.assertEqual(self.client.calls[-1][0], "status")
        group = InboundMessage("telegram", "group:1", "42", "-100", "状态", chat_type="group")
        self.assertEqual(self.processor.process(group).outcome, "blocked")

    def test_help_unknown_and_forbidden_replies_never_delegate_free_form_text(self) -> None:
        self.assertIn("可用命令", self.processor.process(self.message("帮助")).text)
        self.assertEqual(self.processor.process(self.message("随便聊聊")).command_name, "unknown")
        blocked = self.processor.process(self.message("启动 Rocket"))
        self.assertEqual(blocked.outcome, "blocked")
        self.assertIn("不在远程控制范围", blocked.text)
        self.assertFalse(self.client.calls)

    def test_unauthorized_sender_cannot_query_or_control(self) -> None:
        reply = self.processor.process(self.message("状态", sender="99"))
        self.assertEqual(reply.outcome, "blocked")
        self.assertFalse(self.client.calls)

    def test_telegram_restart_requires_bound_callback(self) -> None:
        reply = self.processor.process(self.message("重启 QMT"))
        self.assertEqual(reply.outcome, "awaiting_confirmation")
        self.assertIn("若 Rocket 正在运行", reply.text)
        self.assertIn("自动恢复仍会保持安全阻断", reply.text)
        callback_data = reply.buttons[0][0][1]
        callback = InboundMessage(
            channel="telegram",
            message_key="telegram:callback:1",
            sender_id="42",
            chat_id="42",
            callback_data=callback_data,
            callback_id="callback-1",
        )
        result = self.processor.process(callback)
        self.assertEqual(result.outcome, "succeeded")
        self.assertEqual(self.client.calls[-1][0], "confirm_restart_qmt")

    def test_telegram_cancel_callback_is_one_time_and_invalid_callbacks_fail_closed(self) -> None:
        reply = self.processor.process(self.message("重启 QMT"))
        cancel_data = reply.buttons[0][1][1]
        callback = InboundMessage(
            channel="telegram",
            message_key="telegram:callback:cancel",
            sender_id="42",
            chat_id="42",
            callback_data=cancel_data,
            callback_id="callback-cancel",
        )
        self.assertEqual(self.processor.process(callback).outcome, "cancelled")
        self.assertEqual(self.processor.process(callback).outcome, "blocked")
        invalid = InboundMessage(
            channel="telegram",
            message_key="telegram:callback:invalid",
            sender_id="42",
            chat_id="42",
            callback_data="qg:unknown",
            callback_id="callback-invalid",
        )
        self.assertEqual(self.processor.process(invalid).outcome, "blocked")

    def test_weixin_restart_uses_one_time_text_code(self) -> None:
        reply = self.processor.process(
            self.message("重启 QMT", channel="weixin", sender="wx-owner")
        )
        code = reply.text.rsplit(" ", 1)[-1]
        self.assertRegex(code, r"QG-\d{4}")
        confirmed = self.processor.process(
            self.message(f"确认 {code}", channel="weixin", sender="wx-owner")
        )
        self.assertEqual(confirmed.outcome, "succeeded")

    def test_weixin_confirmation_code_can_be_cancelled_and_not_reused(self) -> None:
        reply = self.processor.process(
            self.message("重启 QMT", channel="weixin", sender="wx-owner")
        )
        code = reply.text.rsplit(" ", 1)[-1]
        cancelled = self.processor.process(
            self.message(f"取消 {code}", channel="weixin", sender="wx-owner")
        )
        self.assertEqual(cancelled.outcome, "cancelled")

        def reject_consumed(*_args, **_kwargs):
            raise GatewayIpcError("确认请求已使用或已失效")

        self.client.request = reject_consumed
        reused = self.processor.process(
            self.message(f"确认 {code}", channel="weixin", sender="wx-owner")
        )
        self.assertEqual(reused.outcome, "blocked")
        missing = self.processor.process(
            self.message("取消", channel="weixin", sender="wx-owner")
        )
        self.assertEqual(missing.outcome, "blocked")

    def test_pairing_code_binds_first_private_chat(self) -> None:
        config = MessagingConfig(gateway_enabled=True)
        config.telegram.enabled = True
        save_messaging_config(config, self.path)
        pairing = self.store.create_pairing(channel="telegram", ttl_seconds=300)
        self.assertRegex(pairing.code, r"^QGP-[A-F0-9]{4}-[A-F0-9]{4}$")
        reply = self.processor.process(self.message(f"绑定 {pairing.code}", sender="77"))
        self.assertEqual(reply.outcome, "succeeded")

    def test_idempotent_blocked_restart_is_not_reported_as_success(self) -> None:
        message = self.message("重启 QMT")
        reply = self.processor.process(message)
        callback_data = reply.buttons[0][0][1]
        self.client.request = lambda *_args, **_kwargs: {
            "status": "blocked",
            "reason": "网络不可用",
            "idempotent": True,
        }
        callback = InboundMessage(
            channel="telegram",
            message_key="telegram:callback:blocked",
            sender_id="42",
            chat_id="42",
            callback_data=callback_data,
            callback_id="callback-blocked",
        )
        result = self.processor.process(callback)
        self.assertEqual(result.outcome, "blocked")
        self.assertIn("网络不可用", result.text)

    def test_remote_restart_disabled_never_creates_a_challenge(self) -> None:
        config = load_messaging_config(self.path)
        config.remote_control.enabled = False
        save_messaging_config(config, self.path)
        reply = self.processor.process(self.message("重启 QMT"))
        self.assertEqual(reply.outcome, "blocked")
        self.assertFalse(self.client.calls)

    def test_incident_and_operation_lists_are_human_readable(self) -> None:
        def request(action, **kwargs):
            self.client.calls.append((action, kwargs))
            if action == "incidents":
                return {
                    "incidents": [
                        {
                            "time": "2026-08-19T10:05:00+08:00",
                            "severity": "warning",
                            "summary": "XTQuant连接中断",
                        }
                    ]
                }
            if action == "operations":
                return {
                    "operations": [
                        {
                            "started_at": "2026-08-19T10:06:00+08:00",
                            "operation_type": "qmt_restart",
                            "status": "succeeded",
                            "initiator": "manual",
                        }
                    ]
                }
            raise AssertionError(action)

        self.client.request = request
        self.assertIn("XTQuant连接中断", self.processor.process(self.message("故障")).text)
        self.assertIn("qmt_restart", self.processor.process(self.message("操作")).text)

    def test_ipc_failure_is_returned_and_recorded_without_retrying_another_action(self) -> None:
        def fail(*_args, **_kwargs):
            raise GatewayIpcError("Guardian IPC unavailable")

        self.client.request = fail
        reply = self.processor.process(self.message("状态"))
        self.assertEqual(reply.outcome, "failed")
        self.assertIn("IPC unavailable", reply.text)
        rows = self.store.activity(limit=10)
        self.assertEqual(rows[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
