from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from quant_guardian.gateway.store import GatewayStore


class GatewayStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self.temporary.name) / "gateway.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_outbox_is_durable_idempotent_and_retryable(self) -> None:
        inserted = self.store.enqueue_outbound(
            channel="telegram",
            chat_id="42",
            text="hello",
            idempotency_key="event:1",
        )
        duplicate = self.store.enqueue_outbound(
            channel="telegram",
            chat_id="42",
            text="hello again",
            idempotency_key="event:1",
        )
        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        [message] = self.store.claim_outbound("telegram")
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.attempts, 1)
        self.store.complete_outbound(message.message_id, success=False, retry_seconds=0)
        self.assertEqual(self.store.stats()["deliveries_failed"], 1)

    def test_inbound_deduplication(self) -> None:
        values = dict(
            message_key="telegram:update:1",
            channel="telegram",
            sender_id="42",
            chat_id="42",
            text_hash="abc",
        )
        self.assertTrue(self.store.record_inbound_once(**values))
        self.assertFalse(self.store.record_inbound_once(**values))

    def test_confirmation_is_bound_to_channel_sender_code_and_single_use(self) -> None:
        challenge = self.store.create_challenge(
            channel="weixin",
            sender_id="wxid-owner",
            chat_id="wxid-owner",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=True,
        )
        value, reason = self.store.consume_challenge(
            challenge_id=challenge.challenge_id,
            channel="weixin",
            sender_id="wrong",
            code=challenge.code,
        )
        self.assertIsNone(value)
        self.assertIn("身份", reason)
        value, reason = self.store.consume_challenge(
            challenge_id=challenge.challenge_id,
            channel="weixin",
            sender_id="wxid-owner",
            code="QG-0000",
        )
        self.assertIsNone(value)
        self.assertIn("不匹配", reason)
        value, _ = self.store.consume_challenge(
            challenge_id=challenge.challenge_id,
            channel="weixin",
            sender_id="wxid-owner",
            code=challenge.code,
        )
        self.assertIsNotNone(value)
        replay, _ = self.store.consume_challenge(
            challenge_id=challenge.challenge_id,
            channel="weixin",
            sender_id="wxid-owner",
            code=challenge.code,
        )
        self.assertIsNone(replay)

    def test_pairing_claims_unknown_private_sender_once(self) -> None:
        pairing = self.store.create_pairing(channel="telegram", ttl_seconds=300)
        claimed, _ = self.store.consume_pairing(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            code=pairing.code,
        )
        self.assertIsNotNone(claimed)
        replay, _ = self.store.consume_pairing(
            channel="telegram",
            sender_id="99",
            chat_id="99",
            code=pairing.code,
        )
        self.assertIsNone(replay)

    def test_stats_include_remote_commands(self) -> None:
        self.store.record_command(
            request_id="one",
            channel="telegram",
            sender_id="42",
            chat_id="42",
            command="restart_qmt",
            status="succeeded",
        )
        stats = self.store.stats(since=datetime.now().astimezone() - timedelta(minutes=1))
        self.assertEqual(stats["commands_total"], 1)
        self.assertEqual(stats["remote_restarts"], 1)
        self.assertEqual(stats["commands_by_channel"]["telegram"], 1)


if __name__ == "__main__":
    unittest.main()
