from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from quant_guardian.gateway.channels.base import ChannelAdapter, ChannelError
from quant_guardian.gateway.channels.telegram import TelegramAdapter
from quant_guardian.gateway.channels.weixin import WeixinAdapter
from quant_guardian.gateway.commands import CommandProcessor, inbound_text_hash
from quant_guardian.gateway.config import (
    default_messaging_config_path,
    load_messaging_config,
)
from quant_guardian.gateway.ipc import GuardianControlClient
from quant_guardian.gateway.models import InboundMessage
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore

CHANNEL_STOP_TIMEOUT_SECONDS = 70.0


class GatewayRuntime:
    def __init__(
        self,
        *,
        config_path: Path | None = None,
        vault: CredentialVault | None = None,
        store: GatewayStore | None = None,
        client: GuardianControlClient | None = None,
        adapters: dict[str, ChannelAdapter] | None = None,
    ) -> None:
        self.config_path = config_path or default_messaging_config_path()
        self.vault = vault or CredentialVault()
        self.store = store or GatewayStore()
        self.client = client or GuardianControlClient(vault=self.vault)
        self.processor = CommandProcessor(
            store=self.store,
            client=self.client,
            config_path=self.config_path,
        )
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._channel_threads: dict[str, threading.Thread] = {}
        self._channel_stops: dict[str, threading.Event] = {}
        self._signatures: dict[str, tuple[str, ...]] = {}
        self._stopping_channels: set[str] = set()
        self._dispatcher: threading.Thread | None = None
        self._injected_adapters = adapters
        self.adapters: dict[str, ChannelAdapter] = {}

    def _build_adapters(self, config: object) -> dict[str, ChannelAdapter]:
        values: dict[str, ChannelAdapter] = {}
        if not getattr(config, "gateway_enabled", False):
            return values
        if config.telegram.enabled:
            values["telegram"] = TelegramAdapter(
                config.telegram,
                token=self.vault.get("telegram_bot_token"),
                store=self.store,
            )
        if config.weixin.enabled:
            values["weixin"] = WeixinAdapter(
                config.weixin,
                token=self.vault.get("weixin_bot_token"),
                store=self.store,
                vault=self.vault,
            )
        return values

    def _desired(self) -> tuple[dict[str, ChannelAdapter], dict[str, tuple[str, ...]]]:
        if self._injected_adapters is not None:
            adapters = dict(self._injected_adapters)
            return adapters, {
                name: (name, "injected", str(id(adapter)))
                for name, adapter in adapters.items()
            }
        config = load_messaging_config(self.config_path)
        adapters = self._build_adapters(config)
        telegram_token = self.vault.get("telegram_bot_token")
        weixin_token = self.vault.get("weixin_bot_token")
        signatures: dict[str, tuple[str, ...]] = {}
        if "telegram" in adapters:
            signatures["telegram"] = (
                hashlib.sha256(telegram_token.encode("utf-8")).hexdigest(),
                str(config.telegram.poll_timeout_seconds),
            )
        if "weixin" in adapters:
            signatures["weixin"] = (
                hashlib.sha256(weixin_token.encode("utf-8")).hexdigest(),
                config.weixin.account_id,
                config.weixin.base_url,
                str(config.weixin.poll_timeout_seconds),
            )
        return adapters, signatures

    def sync_config(self) -> None:
        desired, signatures = self._desired()
        with self._lock:
            changed = {
                name
                for name in set(self.adapters) | set(desired)
                if name not in desired
                or name not in self.adapters
                or self._signatures.get(name) != signatures.get(name)
            }
        stopped = {name for name in changed if self._stop_channel(name)}
        for name in stopped:
            adapter = desired.get(name)
            if adapter is not None:
                self._start_channel(name, adapter, signatures[name])

    def _start_channel(
        self,
        name: str,
        adapter: ChannelAdapter,
        signature: tuple[str, ...],
    ) -> None:
        stop_event = threading.Event()
        thread = threading.Thread(
            target=adapter.run,
            args=(stop_event, self._handle),
            name=f"quant-guardian-{name}",
            daemon=True,
        )
        with self._lock:
            self._stopping_channels.discard(name)
            self.adapters[name] = adapter
            self._signatures[name] = signature
            self._channel_stops[name] = stop_event
            self._channel_threads[name] = thread
        thread.start()

    def _stop_channel(self, name: str) -> bool:
        with self._lock:
            stop_event = self._channel_stops.get(name)
            thread = self._channel_threads.get(name)
            if thread is not None:
                self._stopping_channels.add(name)
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=CHANNEL_STOP_TIMEOUT_SECONDS)
        if thread is not None and thread.is_alive():
            self.store.update_channel_state(
                name,
                "disconnected",
                error="channel reload is waiting for the previous poll to stop",
            )
            return False
        with self._lock:
            self._channel_stops.pop(name, None)
            self._channel_threads.pop(name, None)
            self.adapters.pop(name, None)
            self._signatures.pop(name, None)
            self._stopping_channels.discard(name)
        return True

    def start(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        self._stop.clear()
        self.sync_config()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="quant-guardian-message-dispatch",
            daemon=True,
        )
        self._dispatcher.start()

    def stop(self) -> None:
        self._stop.set()
        for name in tuple(self._channel_threads):
            self._stop_channel(name)
        if self._dispatcher and self._dispatcher.is_alive():
            self._dispatcher.join(timeout=10)
        self._dispatcher = None

    @property
    def running(self) -> bool:
        return bool(
            (self._dispatcher and self._dispatcher.is_alive())
            or any(thread.is_alive() for thread in self._channel_threads.values())
        )

    def _handle(self, message: InboundMessage) -> None:
        if not self.store.record_inbound_once(
            message_key=message.message_key,
            channel=message.channel,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            text_hash=inbound_text_hash(message.text or message.callback_data),
        ):
            return
        try:
            reply = self.processor.process(message)
            self.store.finish_inbound(message.message_key, reply.command_name, reply.outcome)
            self.store.enqueue_outbound(
                channel=message.channel,
                chat_id=message.chat_id,
                text=reply.text,
                buttons=reply.buttons,
                priority=10,
                idempotency_key=f"reply:{message.message_key}",
            )
        except Exception as exc:  # command errors must not kill polling
            self.store.finish_inbound(message.message_key, "internal_error", "failed")
            self.store.enqueue_outbound(
                channel=message.channel,
                chat_id=message.chat_id,
                text=f"命令处理失败：{type(exc).__name__}",
                priority=10,
                idempotency_key=f"error:{message.message_key}",
            )

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            had_work = False
            with self._lock:
                adapters = tuple(
                    (name, adapter)
                    for name, adapter in self.adapters.items()
                    if name not in self._stopping_channels
                )
            for channel, adapter in adapters:
                for message in self.store.claim_outbound(channel, limit=20):
                    had_work = True
                    try:
                        adapter.send(message)
                    except ChannelError as exc:
                        retry = min(300, 5 * (2 ** min(message.attempts, 6)))
                        self.store.complete_outbound(
                            message.message_id,
                            success=False,
                            error=str(exc),
                            retry_seconds=retry if message.attempts < 6 else 0,
                        )
                    except Exception as exc:
                        self.store.complete_outbound(
                            message.message_id,
                            success=False,
                            error=f"{type(exc).__name__}",
                            retry_seconds=30 if message.attempts < 3 else 0,
                        )
                    else:
                        self.store.complete_outbound(message.message_id, success=True)
            self._stop.wait(0.25 if had_work else 1.0)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            self.stop()
