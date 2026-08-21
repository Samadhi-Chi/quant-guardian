from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from quant_guardian.gateway.channels.base import AuthenticationError, ChannelAdapter, ChannelError
from quant_guardian.gateway.channels.https import open_trusted_https
from quant_guardian.gateway.config import TelegramGatewayConfig
from quant_guardian.gateway.models import InboundMessage, OutboundMessage
from quant_guardian.gateway.store import GatewayStore

MAX_API_RESPONSE_BYTES = 2_000_000
_BOT_TOKEN = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{20,256}$")


class TelegramAdapter(ChannelAdapter):
    name = "telegram"
    API_ROOT = "https://api.telegram.org"

    def __init__(
        self,
        config: TelegramGatewayConfig,
        *,
        token: str,
        store: GatewayStore,
    ) -> None:
        self.config = config
        self._token = token.strip()
        self.store = store

    def _api(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 20,
    ) -> dict[str, Any]:
        if not self._token:
            raise AuthenticationError("Telegram bot token is missing")
        if not _BOT_TOKEN.fullmatch(self._token):
            raise AuthenticationError("Telegram bot token format is invalid")
        request = urllib.request.Request(
            f"{self.API_ROOT}/bot{self._token}/{method}",
            data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Quant-Guardian-Gateway/1",
            },
            method="POST",
        )
        try:
            with open_trusted_https(
                request,
                timeout=timeout,
                host_allowed=lambda host: host == "api.telegram.org",
            ) as response:
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw) > MAX_API_RESPONSE_BYTES:
                    raise ChannelError("Telegram response exceeded the safety limit")
                document = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 404}:
                raise AuthenticationError("Telegram rejected the bot token") from exc
            raise ChannelError(f"Telegram HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ChannelError(f"Telegram network error: {type(exc).__name__}") from exc
        if not isinstance(document, dict) or not document.get("ok"):
            description = str(document.get("description") or "Bot API request failed")
            raise ChannelError(f"Telegram API error: {description[:180]}")
        result = document.get("result")
        return result if isinstance(result, dict) else {"items": result}

    def test_connection(self) -> dict[str, str]:
        result = self._api("getMe")
        return {
            "id": str(result.get("id") or ""),
            "username": str(result.get("username") or ""),
            "name": str(result.get("first_name") or ""),
        }

    def run(
        self,
        stop_event: threading.Event,
        on_message: Callable[[InboundMessage], None],
    ) -> None:
        failures = 0
        while not stop_event.is_set():
            try:
                identity = self.test_connection()
                self.store.update_channel_state(
                    self.name,
                    "connected",
                    identity=(
                        "@" + identity["username"] if identity["username"] else identity["id"]
                    ),
                    reconnected=failures > 0,
                )
                failures = 0
                self._poll(stop_event, on_message)
            except AuthenticationError as exc:
                self.store.update_channel_state(self.name, "auth_required", error=str(exc))
                stop_event.wait(60)
            except ChannelError as exc:
                failures += 1
                self.store.update_channel_state(
                    self.name,
                    "disconnected",
                    error=str(exc),
                    reconnected=True,
                )
                stop_event.wait(min(60, 2 ** min(failures, 5)))

    def _poll(
        self,
        stop_event: threading.Event,
        on_message: Callable[[InboundMessage], None],
    ) -> None:
        offset = int(self.store.get_meta("telegram.update_offset", "0") or 0)
        while not stop_event.is_set():
            result = self._api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": self.config.poll_timeout_seconds,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=self.config.poll_timeout_seconds + 12,
            )
            updates = result.get("items") if "items" in result else result
            if not isinstance(updates, list):
                updates = []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = int(update.get("update_id") or 0)
                message = self._inbound(update)
                if message is not None:
                    on_message(message)
                    if message.callback_id:
                        self.answer_callback(message.callback_id, "请求已接收")
                    self.store.update_channel_state(self.name, "connected", received=True)
                offset = max(offset, update_id + 1)
                self.store.set_meta("telegram.update_offset", str(offset))

    def _inbound(self, update: dict[str, Any]) -> InboundMessage | None:
        update_id = str(update.get("update_id") or "")
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
            source = callback.get("message") if isinstance(callback.get("message"), dict) else {}
            chat = source.get("chat") if isinstance(source.get("chat"), dict) else {}
            if str(chat.get("type") or "") != "private":
                return None
            return InboundMessage(
                channel=self.name,
                message_key=f"telegram:update:{update_id}",
                sender_id=str(sender.get("id") or ""),
                chat_id=str(chat.get("id") or ""),
                callback_id=str(callback.get("id") or ""),
                callback_data=str(callback.get("data") or ""),
            )
        source = update.get("message")
        if not isinstance(source, dict):
            return None
        chat = source.get("chat") if isinstance(source.get("chat"), dict) else {}
        sender = source.get("from") if isinstance(source.get("from"), dict) else {}
        if str(chat.get("type") or "") != "private":
            return None
        text = str(source.get("text") or "")[:8_000]
        if not text:
            return None
        return InboundMessage(
            channel=self.name,
            message_key=f"telegram:update:{update_id}",
            sender_id=str(sender.get("id") or ""),
            chat_id=str(chat.get("id") or ""),
            text=text,
        )

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            self._api(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text[:180]},
                timeout=10,
            )
        except ChannelError:
            return

    @staticmethod
    def _chunks(text: str, limit: int = 3500) -> list[str]:
        remaining = text.strip()
        chunks: list[str] = []
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split = remaining.rfind("\n", 0, limit)
            if split < limit // 2:
                split = limit
            chunks.append(remaining[:split].rstrip())
            remaining = remaining[split:].lstrip()
        return chunks or [""]

    def send(self, message: OutboundMessage) -> str:
        sent_id = ""
        chunks = self._chunks(message.text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": message.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if index == len(chunks) - 1 and message.buttons:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [{"text": label, "callback_data": value} for label, value in group]
                        for group in message.buttons
                    ]
                }
            result = self._api("sendMessage", payload, timeout=20)
            sent_id = str(result.get("message_id") or sent_id)
            if index < len(chunks) - 1:
                time.sleep(0.15)
        self.store.update_channel_state(self.name, "connected", sent=True)
        return sent_id
