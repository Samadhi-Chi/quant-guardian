from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.gateway.channels.base import ChannelAdapter
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


if __name__ == "__main__":
    unittest.main()
