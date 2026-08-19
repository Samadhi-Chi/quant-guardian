"""Minimal text-only Weixin iLink Bot adapter.

Protocol behavior is independently adapted from Nous Research Hermes Agent's
MIT-licensed Weixin adapter.  Quant Guardian deliberately excludes Hermes'
agent, shell, media, voice, file, and group-chat capabilities.
"""

from __future__ import annotations

import base64
import json
import secrets
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from quant_guardian.gateway.channels.base import AuthenticationError, ChannelAdapter, ChannelError
from quant_guardian.gateway.channels.https import open_trusted_https
from quant_guardian.gateway.config import WeixinGatewayConfig, is_trusted_weixin_base_url
from quant_guardian.gateway.models import InboundMessage, OutboundMessage
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
SESSION_EXPIRED_ERRCODE = -14
MAX_API_RESPONSE_BYTES = 2_000_000
MAX_QR_CONTENT_LENGTH = 4_096


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: str = "", *, length: int = 0) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(length),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "User-Agent": "Quant-Guardian-Gateway/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request_json(
    *,
    base_url: str,
    endpoint: str,
    token: str = "",
    payload: dict[str, Any] | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    if not is_trusted_weixin_base_url(base_url):
        raise ChannelError("Weixin iLink endpoint is not trusted")
    if payload is None:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/{endpoint}",
            headers={
                "iLink-App-Id": ILINK_APP_ID,
                "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
                "User-Agent": "Quant-Guardian-Gateway/1",
            },
            method="GET",
        )
    else:
        body = json.dumps(
            {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/{endpoint}",
            data=body,
            headers=_headers(token, length=len(body)),
            method="POST",
        )
    try:
        with open_trusted_https(
            request,
            timeout=timeout,
            host_allowed=lambda host: host == "weixin.qq.com" or host.endswith(".weixin.qq.com"),
        ) as response:
            raw = response.read(MAX_API_RESPONSE_BYTES + 1)
            if len(raw) > MAX_API_RESPONSE_BYTES:
                raise ChannelError("Weixin iLink response exceeded the safety limit")
            document = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthenticationError("Weixin iLink rejected the saved login") from exc
        raise ChannelError(f"Weixin iLink HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ChannelError(f"Weixin iLink network error: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise ChannelError("Weixin iLink returned an invalid response")
    return document


def request_qr_code(*, bot_type: str = "3") -> dict[str, str]:
    response = _request_json(
        base_url=ILINK_BASE_URL,
        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
        timeout=35,
    )
    token = str(response.get("qrcode") or "")
    content = str(response.get("qrcode_img_content") or token)
    if (
        not token
        or not content
        or len(token) > MAX_QR_CONTENT_LENGTH
        or len(content) > MAX_QR_CONTENT_LENGTH
    ):
        raise ChannelError("Weixin QR response was incomplete")
    return {"qrcode": token, "content": content, "base_url": ILINK_BASE_URL}


def poll_qr_code(qrcode: str, *, base_url: str = ILINK_BASE_URL) -> dict[str, str]:
    response = _request_json(
        base_url=base_url,
        endpoint=f"{EP_GET_QR_STATUS}?qrcode={urllib.parse.quote(qrcode, safe='')}",
        timeout=35,
    )
    return {
        "status": str(response.get("status") or "wait"),
        "account_id": str(response.get("ilink_bot_id") or ""),
        "token": str(response.get("bot_token") or ""),
        "base_url": str(response.get("baseurl") or base_url),
        "user_id": str(response.get("ilink_user_id") or ""),
        "redirect_host": str(response.get("redirect_host") or ""),
    }


def _extract_text(items: list[Any]) -> str:
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == ITEM_TEXT:
            text_item = item.get("text_item") if isinstance(item.get("text_item"), dict) else {}
            value = str(text_item.get("text") or "").strip()
            if value:
                values.append(value)
        reference = item.get("ref_msg") if isinstance(item.get("ref_msg"), dict) else {}
        ref_item = reference.get("message_item")
        if isinstance(ref_item, dict) and ref_item.get("type") == ITEM_TEXT:
            text_item = (
                ref_item.get("text_item") if isinstance(ref_item.get("text_item"), dict) else {}
            )
            value = str(text_item.get("text") or "").strip()
            if value:
                values.append(f"> {value}")
    return "\n".join(values)[:8_000]


class WeixinAdapter(ChannelAdapter):
    name = "weixin"

    def __init__(
        self,
        config: WeixinGatewayConfig,
        *,
        token: str,
        store: GatewayStore,
        vault: CredentialVault,
    ) -> None:
        self.config = config
        self._token = token.strip()
        self.store = store
        self.vault = vault
        self._context: dict[str, str] = {}

    def _context_key(self, peer: str) -> str:
        import hashlib

        return "weixin_context_" + hashlib.sha256(peer.encode("utf-8")).hexdigest()[:24]

    def _get_context(self, peer: str) -> str:
        if peer not in self._context:
            self._context[peer] = self.vault.get(self._context_key(peer))
        return self._context.get(peer, "")

    def _set_context(self, peer: str, token: str) -> None:
        self._context[peer] = token
        self.vault.set(self._context_key(peer), token)

    def run(
        self,
        stop_event: threading.Event,
        on_message: Callable[[InboundMessage], None],
    ) -> None:
        if not self._token or not self.config.account_id:
            self.store.update_channel_state(
                self.name, "auth_required", error="Weixin QR login is required"
            )
            return
        failures = 0
        sync_buf = self.store.get_meta("weixin.sync_buf", "")
        timeout = self.config.poll_timeout_seconds
        self.store.update_channel_state(
            self.name, "connected", identity=self.config.account_id[:12]
        )
        while not stop_event.is_set():
            try:
                response = _request_json(
                    base_url=self.config.base_url or ILINK_BASE_URL,
                    endpoint=EP_GET_UPDATES,
                    token=self._token,
                    payload={"get_updates_buf": sync_buf},
                    timeout=timeout + 10,
                )
                suggested = response.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and 5_000 <= suggested <= 50_000:
                    timeout = max(5, int(suggested / 1000))
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
                        raise AuthenticationError("Weixin iLink login expired; scan QR again")
                    raise ChannelError(f"Weixin iLink getupdates failed: {ret or errcode}")
                failures = 0
                new_sync = str(response.get("get_updates_buf") or "")
                for raw in response.get("msgs") or []:
                    message = self._inbound(raw)
                    if message is not None:
                        on_message(message)
                        self.store.update_channel_state(self.name, "connected", received=True)
                if new_sync:
                    sync_buf = new_sync
                    self.store.set_meta("weixin.sync_buf", sync_buf)
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
                stop_event.wait(30 if failures >= 3 else 2)

    def _inbound(self, raw: Any) -> InboundMessage | None:
        if not isinstance(raw, dict):
            return None
        sender = str(raw.get("from_user_id") or "").strip()[:256]
        if not sender or sender == self.config.account_id:
            return None
        room_id = str(raw.get("room_id") or raw.get("chat_room_id") or "").strip()
        to_user = str(raw.get("to_user_id") or "").strip()
        is_group = bool(room_id) or (
            to_user
            and self.config.account_id
            and to_user != self.config.account_id
            and raw.get("msg_type") == 1
        )
        if is_group:
            return None
        context_token = str(raw.get("context_token") or "").strip()[:8_192]
        if context_token:
            self._set_context(sender, context_token)
        text = _extract_text(list(raw.get("item_list") or []))
        if not text:
            return None
        message_id = str(raw.get("message_id") or "")[:256]
        if not message_id:
            import hashlib

            message_id = hashlib.sha256(
                f"{sender}|{text}|{context_token}".encode("utf-8")
            ).hexdigest()[:32]
        return InboundMessage(
            channel=self.name,
            message_key=f"weixin:message:{message_id}",
            sender_id=sender,
            chat_id=sender,
            text=text,
        )

    @staticmethod
    def _chunks(text: str, limit: int = 1800) -> list[str]:
        value = text.strip()
        chunks: list[str] = []
        while value:
            if len(value) <= limit:
                chunks.append(value)
                break
            split = value.rfind("\n", 0, limit)
            if split < limit // 2:
                split = limit
            chunks.append(value[:split].rstrip())
            value = value[split:].lstrip()
        return chunks or [""]

    def send(self, message: OutboundMessage) -> str:
        if not self._token:
            raise AuthenticationError("Weixin login is missing")
        context = self._get_context(message.chat_id)
        last_id = ""
        for index, chunk in enumerate(self._chunks(message.text)):
            client_id = f"quant-guardian-weixin-{uuid.uuid4().hex}"
            body: dict[str, Any] = {
                "from_user_id": "",
                "to_user_id": message.chat_id,
                "client_id": client_id,
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": chunk}}],
            }
            if context:
                body["context_token"] = context
            response = _request_json(
                base_url=self.config.base_url or ILINK_BASE_URL,
                endpoint=EP_SEND_MESSAGE,
                token=self._token,
                payload={"msg": body},
                timeout=20,
            )
            ret = response.get("ret", 0)
            errcode = response.get("errcode", 0)
            if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE) and context:
                self.vault.delete(self._context_key(message.chat_id))
                self._context[message.chat_id] = ""
                context = ""
                body.pop("context_token", None)
                response = _request_json(
                    base_url=self.config.base_url or ILINK_BASE_URL,
                    endpoint=EP_SEND_MESSAGE,
                    token=self._token,
                    payload={"msg": body},
                    timeout=20,
                )
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
            if ret not in {0, None} or errcode not in {0, None}:
                raise ChannelError(f"Weixin iLink send failed: {ret or errcode}")
            last_id = client_id
            if index < len(self._chunks(message.text)) - 1:
                time.sleep(1.0)
        self.store.update_channel_state(self.name, "connected", sent=True)
        return last_id
