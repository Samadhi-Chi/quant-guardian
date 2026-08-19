from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from quant_guardian.gateway.bridge import GatewayEventBridge
from quant_guardian.gateway.config import MessagingConfig, save_messaging_config
from quant_guardian.gateway.privacy import safe_message_text
from quant_guardian.gateway.store import GatewayStore
from quant_guardian.notifications import Notification


class GatewayBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.path = root / "messaging.json"
        self.store = GatewayStore(root / "gateway.db")
        self.config = MessagingConfig(gateway_enabled=True)
        self.config.telegram.enabled = True
        self.config.telegram.allowed_user_ids = ["42"]
        self.config.telegram.home_chat_id = "42"
        self.config.broadcast.enabled = True
        save_messaging_config(self.config, self.path)
        self.bridge = GatewayEventBridge(self.store, config_path=self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def notification(self, key: str, severity: str = "warning") -> Notification:
        return Notification(
            "需要关注",
            r"日志 C:\Users\sheng\secret\qmt.log token=abc123",
            severity,
            key,
            datetime.now().astimezone(),
        )

    def test_health_switch_and_severity_are_enforced(self) -> None:
        self.config.broadcast.health_events = False
        save_messaging_config(self.config, self.path)
        self.bridge.on_notification(self.notification("trade_system:zeus:critical"))
        self.assertFalse(self.store.claim_outbound("telegram"))

        self.config.broadcast.health_events = True
        self.config.broadcast.minimum_severity = "critical"
        save_messaging_config(self.config, self.path)
        self.bridge.on_notification(self.notification("trade_system:zeus:warning"))
        self.assertFalse(self.store.claim_outbound("telegram"))
        self.bridge.on_notification(self.notification("trade_system:zeus:critical", "critical"))
        messages = self.store.claim_outbound("telegram")
        self.assertEqual(len(messages), 1)
        self.assertNotIn(r"C:\Users\sheng", messages[0].text)
        self.assertIn("<LOCAL_PATH>", messages[0].text)

    def test_operation_event_flags_and_success_option_are_enforced(self) -> None:
        now = datetime.now().astimezone().isoformat()
        recovery = {
            "event_type": "recovery_verified",
            "event_id": "QGO-1",
            "time": now,
            "severity": "info",
            "payload": {"status": "succeeded", "reason": "stable"},
        }
        self.config.broadcast.include_healthy_recovery = False
        save_messaging_config(self.config, self.path)
        self.bridge.on_audit(recovery)
        self.assertFalse(self.store.claim_outbound("telegram"))

        self.config.broadcast.include_healthy_recovery = True
        save_messaging_config(self.config, self.path)
        self.bridge.on_audit(recovery)
        self.assertEqual(len(self.store.claim_outbound("telegram")), 1)

        self.config.broadcast.operation_events = False
        save_messaging_config(self.config, self.path)
        self.bridge.on_audit(
            {
                **recovery,
                "event_type": "manual_qmt_restart_result",
                "event_id": "QGO-2",
            }
        )
        self.assertFalse(self.store.claim_outbound("telegram"))

    def test_recovery_notification_duplicate_is_suppressed(self) -> None:
        self.bridge.on_notification(self.notification("manual_qmt_restart_result", "critical"))
        self.assertFalse(self.store.claim_outbound("telegram"))

    def test_message_sanitizer_masks_standalone_bot_token(self) -> None:
        token = "123456789:" + "AAA" + ("b" * 24)
        self.assertNotIn(token, safe_message_text("failed " + token))


if __name__ == "__main__":
    unittest.main()
