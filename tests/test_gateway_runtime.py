from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.gateway.channels.base import ChannelAdapter, UserActionRequired
from quant_guardian.gateway.config import MessagingConfig, save_messaging_config
from quant_guardian.gateway.models import InboundMessage, OutboundMessage
from quant_guardian.gateway.runtime import GatewayRuntime
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore


class FakeAdapter(ChannelAdapter):
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run(self, stop_event, on_message) -> None:
        del on_message
        self.started.set()
        stop_event.wait(2)
        self.stopped.set()

    def send(self, message: OutboundMessage) -> str:
        return message.message_id


class StubbornAdapter(FakeAdapter):
    def run(self, stop_event, on_message) -> None:
        del stop_event, on_message
        self.started.set()
        time.sleep(0.15)
        self.stopped.set()


class CrashOnceAdapter(FakeAdapter):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.run_count = 0
        self.restarted = threading.Event()

    def run(self, stop_event, on_message) -> None:
        del on_message
        self.run_count += 1
        self.started.set()
        if self.run_count == 1:
            return
        self.restarted.set()
        stop_event.wait(2)
        self.stopped.set()


class AuthRequiredAdapter(FakeAdapter):
    def __init__(self, name: str, store: GatewayStore) -> None:
        super().__init__(name)
        self.store = store
        self.run_count = 0

    def run(self, stop_event, on_message) -> None:
        del stop_event, on_message
        self.run_count += 1
        self.started.set()
        self.store.update_channel_state(
            self.name,
            "auth_required",
            error="login required",
        )


class UserActionAdapter(FakeAdapter):
    def send(self, message: OutboundMessage) -> str:
        del message
        raise UserActionRequired("send any message to refresh the channel context")


class GatewayRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = GatewayStore(root / "gateway.db")
        self.vault = CredentialVault(
            root / "secrets.json",
            protect=lambda value: "x" + value,
            unprotect=lambda value: value[1:],
        )
        self.adapters: dict[str, ChannelAdapter] = {}
        self.runtime = GatewayRuntime(
            config_path=root / "messaging.json",
            vault=self.vault,
            store=self.store,
            adapters=self.adapters,
        )

    def tearDown(self) -> None:
        self.runtime.stop()
        self.temporary.cleanup()

    def test_hot_add_replace_and_remove_channel_without_mutating_source(self) -> None:
        first = FakeAdapter("telegram")
        self.adapters["telegram"] = first
        self.runtime.start()
        self.assertTrue(first.started.wait(1))
        self.assertIn("telegram", self.adapters)

        second = FakeAdapter("telegram")
        self.adapters["telegram"] = second
        self.runtime.sync_config()
        self.assertTrue(first.stopped.wait(1))
        self.assertTrue(second.started.wait(1))

        self.adapters.pop("telegram")
        self.runtime.sync_config()
        self.assertTrue(second.stopped.wait(1))
        self.assertFalse(self.runtime.adapters)

    def test_duplicate_inbound_message_is_processed_only_once(self) -> None:
        message = InboundMessage(
            channel="telegram",
            message_key="telegram:update:1",
            sender_id="42",
            chat_id="42",
            text="状态",
        )
        calls = []

        def process(value):
            calls.append(value)
            return type(
                "Reply",
                (),
                {
                    "command_name": "status",
                    "outcome": "succeeded",
                    "text": "ok",
                    "buttons": (),
                },
            )()

        self.runtime.processor.process = process
        self.runtime._handle(message)
        self.runtime._handle(message)
        time.sleep(0.02)
        self.assertEqual(len(calls), 1)

    def test_replacement_never_overlaps_a_channel_that_has_not_stopped(self) -> None:
        first = StubbornAdapter("weixin")
        second = FakeAdapter("weixin")
        self.adapters["weixin"] = first
        self.runtime.start()
        self.assertTrue(first.started.wait(1))
        self.adapters["weixin"] = second
        with patch("quant_guardian.gateway.runtime.CHANNEL_STOP_TIMEOUT_SECONDS", 0.01):
            self.runtime.sync_config()
        self.assertFalse(second.started.is_set())
        self.assertTrue(first.stopped.wait(1))
        self.runtime.sync_config()
        self.assertTrue(second.started.wait(1))

    def test_real_config_builds_both_adapters_and_stable_signatures(self) -> None:
        root = Path(self.temporary.name)
        config = MessagingConfig(gateway_enabled=True)
        config.telegram.enabled = True
        config.weixin.enabled = True
        config.weixin.account_id = "test-weixin-account"
        save_messaging_config(config, root / "messaging.json")
        self.vault.set("telegram_bot_token", "telegram-test-token")
        self.vault.set("weixin_bot_token", "weixin-test-token")
        runtime = GatewayRuntime(
            config_path=root / "messaging.json",
            vault=self.vault,
            store=self.store,
            adapters=None,
        )

        adapters, signatures = runtime._desired()

        self.assertEqual(set(adapters), {"telegram", "weixin"})
        self.assertEqual(set(signatures), {"telegram", "weixin"})
        self.assertNotIn("telegram-test-token", signatures["telegram"])
        self.assertNotIn("weixin-test-token", signatures["weixin"])

    def test_supervisor_survives_reload_error_and_clears_evidence(self) -> None:
        second_call_started = threading.Event()
        release_second_call = threading.Event()
        with patch("quant_guardian.gateway.runtime.CONFIG_SYNC_INTERVAL_SECONDS", 0.02):
            self.runtime.start()
            original_sync = self.runtime.sync_config
            calls = 0

            def flaky_sync() -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ValueError("test reload failure")
                second_call_started.set()
                release_second_call.wait(1)
                original_sync()

            with patch.object(self.runtime, "sync_config", side_effect=flaky_sync):
                self.assertTrue(second_call_started.wait(1))
                self.assertIn(
                    "ValueError: test reload failure",
                    self.store.get_meta("gateway.supervisor_error"),
                )
                self.assertTrue(self.runtime.running)
                release_second_call.set()
                deadline = time.monotonic() + 1
                while (
                    self.store.get_meta("gateway.supervisor_error") and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(self.store.get_meta("gateway.supervisor_error"), "")

    def test_supervisor_hot_adds_channel_without_manual_sync(self) -> None:
        adapter = FakeAdapter("weixin")
        with patch("quant_guardian.gateway.runtime.CONFIG_SYNC_INTERVAL_SECONDS", 0.02):
            self.runtime.start()
            self.adapters["weixin"] = adapter
            self.assertTrue(adapter.started.wait(1))

    def test_supervisor_restarts_unexpectedly_exited_worker(self) -> None:
        adapter = CrashOnceAdapter("weixin")
        self.adapters["weixin"] = adapter
        with patch("quant_guardian.gateway.runtime.CONFIG_SYNC_INTERVAL_SECONDS", 0.02):
            self.runtime.start()
            self.assertTrue(adapter.restarted.wait(1))
        state = {item["channel"]: item for item in self.store.channel_states()}
        self.assertGreaterEqual(state["weixin"]["reconnect_count"], 1)

    def test_supervisor_does_not_restart_auth_required_channel(self) -> None:
        adapter = AuthRequiredAdapter("weixin", self.store)
        self.adapters["weixin"] = adapter
        with patch("quant_guardian.gateway.runtime.CONFIG_SYNC_INTERVAL_SECONDS", 0.02):
            self.runtime.start()
            self.assertTrue(adapter.started.wait(1))
            time.sleep(0.08)
        self.assertEqual(adapter.run_count, 1)

    def test_dispatcher_stops_retrying_when_channel_needs_user_action(self) -> None:
        adapter = UserActionAdapter("weixin")
        self.adapters["weixin"] = adapter
        self.store.enqueue_outbound(
            channel="weixin",
            chat_id="owner",
            text="test",
            idempotency_key="test:user-action",
        )
        self.runtime.start()
        deadline = time.monotonic() + 1
        row = {}
        while time.monotonic() < deadline:
            row = next(
                (
                    item
                    for item in self.store.activity(limit=10)
                    if item["item_id"] == "1"
                ),
                {},
            )
            if row.get("status") == "failed":
                break
            time.sleep(0.01)
        self.assertEqual(row.get("status"), "failed")
        state = {item["channel"]: item for item in self.store.channel_states()}["weixin"]
        self.assertEqual(state["status"], "attention_required")
        self.assertIn("refresh", state["last_error"])


if __name__ == "__main__":
    unittest.main()
