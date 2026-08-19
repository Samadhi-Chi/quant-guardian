from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from quant_guardian.gateway.channels.base import AuthenticationError, ChannelError
from quant_guardian.gateway.channels.https import open_trusted_https
from quant_guardian.gateway.channels.telegram import TelegramAdapter
from quant_guardian.gateway.channels.weixin import (
    SESSION_EXPIRED_ERRCODE,
    WeixinAdapter,
    _request_json,
    poll_qr_code,
    request_qr_code,
)
from quant_guardian.gateway.config import TelegramGatewayConfig, WeixinGatewayConfig
from quant_guardian.gateway.models import OutboundMessage
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore

TELEGRAM_TOKEN = "123456789:" + "AAA" + ("b" * 32)


class FakeResponse:
    def __init__(self, document: dict) -> None:
        self.data = json.dumps(document).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.data if size < 0 else self.data[:size]


class GatewayChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = GatewayStore(root / "gateway.db")
        self.vault = CredentialVault(
            root / "secrets.json",
            protect=lambda value: "x" + value[::-1],
            unprotect=lambda value: value[1:][::-1],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_https_transport_rejects_every_untrusted_destination_before_connect(self) -> None:
        destinations = (
            "http://api.telegram.org/bot",
            "https://api.telegram.org.attacker.test/bot",
            "https://user@api.telegram.org/bot",
            "https://api.telegram.org:444/bot",
            "https://api.telegram.org/bot#fragment",
        )
        with patch("http.client.HTTPSConnection") as connection:
            for destination in destinations:
                with (
                    self.subTest(destination=destination),
                    self.assertRaises(urllib.error.URLError),
                ):
                    with open_trusted_https(
                        urllib.request.Request(destination),
                        timeout=1,
                        host_allowed=lambda host: host == "api.telegram.org",
                    ):
                        pass
        connection.assert_not_called()

    def test_https_transport_uses_verified_host_and_origin_form_path(self) -> None:
        response = MagicMock(status=200)
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch("http.client.HTTPSConnection", return_value=connection):
            request = urllib.request.Request(
                "https://api.telegram.org/bot/redacted?offset=1",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with open_trusted_https(
                request,
                timeout=2,
                host_allowed=lambda host: host == "api.telegram.org",
            ) as opened:
                self.assertIs(opened, response)
        connection.request.assert_called_once_with(
            "POST",
            "/bot/redacted?offset=1",
            body=b"{}",
            headers={"Content-type": "application/json"},
        )
        connection.close.assert_called_once_with()

    def test_telegram_long_poll_accepts_private_text_and_persists_offset(self) -> None:
        adapter = TelegramAdapter(
            TelegramGatewayConfig(poll_timeout_seconds=5),
            token=TELEGRAM_TOKEN,
            store=self.store,
        )
        stop = threading.Event()
        calls = []

        def api(method, payload=None, **_kwargs):
            self.assertEqual(method, "getUpdates")
            self.assertEqual(payload["offset"], 0)
            return {
                "items": [
                    {
                        "update_id": 17,
                        "message": {
                            "from": {"id": 42},
                            "chat": {"id": 42, "type": "private"},
                            "text": "状态",
                        },
                    }
                ]
            }

        adapter._api = api

        def received(message):
            calls.append(message)
            stop.set()

        adapter._poll(stop, received)
        self.assertEqual(calls[0].sender_id, "42")
        self.assertEqual(calls[0].text, "状态")
        self.assertEqual(self.store.get_meta("telegram.update_offset"), "18")

    def test_telegram_rejects_group_and_sends_inline_confirmation(self) -> None:
        adapter = TelegramAdapter(TelegramGatewayConfig(), token=TELEGRAM_TOKEN, store=self.store)
        self.assertIsNone(
            adapter._inbound(
                {
                    "update_id": 1,
                    "message": {
                        "from": {"id": 42},
                        "chat": {"id": -1, "type": "group"},
                        "text": "状态",
                    },
                }
            )
        )
        payloads = []

        def api(method, payload=None, **_kwargs):
            self.assertEqual(method, "sendMessage")
            payloads.append(payload)
            return {"message_id": 99}

        adapter._api = api
        adapter.send(
            OutboundMessage(
                1,
                "telegram",
                "42",
                "确认重启？",
                ((("确认", "qg:restart:confirm:QGC-" + "a" * 24),),),
            )
        )
        self.assertEqual(payloads[0]["chat_id"], "42")
        self.assertEqual(payloads[0]["reply_markup"]["inline_keyboard"][0][0]["text"], "确认")

    def test_telegram_auth_error_never_exposes_token(self) -> None:
        token = TELEGRAM_TOKEN
        adapter = TelegramAdapter(TelegramGatewayConfig(), token=token, store=self.store)
        error = urllib.error.HTTPError(
            "https://api.telegram.org/redacted", 401, "Unauthorized", {}, None
        )
        with patch(
            "quant_guardian.gateway.channels.telegram.open_trusted_https",
            side_effect=error,
        ):
            with self.assertRaises(AuthenticationError) as raised:
                adapter.test_connection()
        self.assertNotIn(token, str(raised.exception))

    def test_telegram_rejects_oversized_api_response(self) -> None:
        adapter = TelegramAdapter(TelegramGatewayConfig(), token=TELEGRAM_TOKEN, store=self.store)
        response = FakeResponse({"ok": True, "result": {"value": "x" * 2_000_000}})
        with patch(
            "quant_guardian.gateway.channels.telegram.open_trusted_https",
            return_value=response,
        ):
            with self.assertRaisesRegex(ChannelError, "safety limit"):
                adapter.test_connection()

    def test_telegram_rejects_malformed_token_before_network(self) -> None:
        adapter = TelegramAdapter(TelegramGatewayConfig(), token="not/a/token", store=self.store)
        with patch("quant_guardian.gateway.channels.telegram.open_trusted_https") as urlopen:
            with self.assertRaisesRegex(AuthenticationError, "format"):
                adapter.test_connection()
        urlopen.assert_not_called()

    def test_weixin_qr_calls_are_quoted_and_credentials_are_parsed(self) -> None:
        documents = [
            {"qrcode": "a+b/c=", "qrcode_img_content": "https://qr.invalid/abc"},
            {
                "status": "confirmed",
                "ilink_bot_id": "bot@im.bot",
                "bot_token": "token",
                "ilink_user_id": "owner",
            },
        ]
        requests = []

        def urlopen(request, **_kwargs):
            requests.append(request.full_url)
            return FakeResponse(documents.pop(0))

        with patch(
            "quant_guardian.gateway.channels.weixin.open_trusted_https",
            side_effect=urlopen,
        ):
            qr = request_qr_code()
            result = poll_qr_code(qr["qrcode"])
        self.assertEqual(result["account_id"], "bot@im.bot")
        self.assertIn("qrcode=a%2Bb%2Fc%3D", requests[1])

    def test_weixin_rejects_untrusted_token_destination(self) -> None:
        with self.assertRaises(ChannelError):
            _request_json(
                base_url="https://weixin.qq.com.attacker.test",
                endpoint="ilink/bot/getupdates",
                token="must-not-leak",
                payload={"get_updates_buf": ""},
            )

    def test_weixin_private_text_context_and_group_rejection(self) -> None:
        config = WeixinGatewayConfig(account_id="bot@im.bot")
        adapter = WeixinAdapter(config, token="token", store=self.store, vault=self.vault)
        group = {
            "message_id": "g1",
            "from_user_id": "owner",
            "room_id": "group",
            "item_list": [{"type": 1, "text_item": {"text": "状态"}}],
        }
        self.assertIsNone(adapter._inbound(group))
        direct = {
            "message_id": "d1",
            "from_user_id": "owner",
            "to_user_id": "bot@im.bot",
            "context_token": "context-secret",
            "item_list": [{"type": 1, "text_item": {"text": "状态"}}],
        }
        message = adapter._inbound(direct)
        self.assertIsNotNone(message)
        self.assertEqual(message.chat_id, "owner")
        self.assertEqual(adapter._get_context("owner"), "context-secret")
        self.assertNotIn("context-secret", self.vault.path.read_text(encoding="utf-8"))

    def test_weixin_successful_poll_clears_a_previous_network_error(self) -> None:
        config = WeixinGatewayConfig(account_id="bot@im.bot", poll_timeout_seconds=5)
        adapter = WeixinAdapter(config, token="token", store=self.store, vault=self.vault)
        stop = MagicMock()
        stop.is_set.side_effect = [False, False, True]
        stop.wait.return_value = False
        responses = [
            ChannelError("temporary network failure"),
            {"ret": 0, "get_updates_buf": "next", "msgs": []},
        ]

        with patch(
            "quant_guardian.gateway.channels.weixin._request_json",
            side_effect=responses,
        ):
            adapter.run(stop, lambda _message: None)

        state = {item["channel"]: item for item in self.store.channel_states()}["weixin"]
        self.assertEqual(state["status"], "connected")
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["reconnect_count"], 1)
        self.assertEqual(self.store.get_meta("weixin.sync_buf"), "next")

    def test_weixin_send_retries_without_stale_context(self) -> None:
        config = WeixinGatewayConfig(account_id="bot@im.bot")
        adapter = WeixinAdapter(config, token="token", store=self.store, vault=self.vault)
        adapter._set_context("owner", "old-context")
        payloads = []

        def request(**kwargs):
            payloads.append(json.loads(json.dumps(kwargs["payload"])))
            if len(payloads) == 1:
                return {"errcode": SESSION_EXPIRED_ERRCODE}
            return {"ret": 0}

        with patch("quant_guardian.gateway.channels.weixin._request_json", side_effect=request):
            adapter.send(OutboundMessage(1, "weixin", "owner", "状态正常"))
        self.assertEqual(payloads[0]["msg"]["context_token"], "old-context")
        self.assertNotIn("context_token", payloads[1]["msg"])
        self.assertEqual(adapter._get_context("owner"), "")


if __name__ == "__main__":
    unittest.main()
