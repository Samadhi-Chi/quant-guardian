from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quant_guardian.gateway.config import (
    MessagingConfig,
    load_messaging_config,
    remote_control_authorized,
    save_messaging_config,
    set_remote_control_authorized,
)
from quant_guardian.gateway.secrets import CredentialVault


class GatewayConfigAndSecretsTests(unittest.TestCase):
    def test_messaging_config_roundtrip_and_locked_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messaging.json"
            config = MessagingConfig(gateway_enabled=True)
            config.telegram.enabled = True
            config.telegram.allowed_user_ids = ["42", "42", ""]
            config.telegram.home_chat_id = "42"
            config.remote_control.enabled = True
            save_messaging_config(config, path)

            loaded = load_messaging_config(path)
            self.assertTrue(loaded.gateway_enabled)
            self.assertEqual(loaded.telegram.allowed_user_ids, ["42"])
            self.assertFalse(loaded.weixin.group_enabled)
            self.assertFalse(loaded.remote_control.quantclass_restart_enabled)
            self.assertEqual(loaded.remote_control.confirmation_ttl_seconds, 300)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("token", json.dumps(document).casefold())

    def test_weixin_endpoint_must_be_trusted_https_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messaging.json"
            config = MessagingConfig()
            config.weixin.base_url = "https://weixin.qq.com.attacker.test"
            with self.assertRaisesRegex(ValueError, "weixin.base_url"):
                save_messaging_config(config, path)

    def test_invalid_group_or_quantclass_control_is_rejected(self) -> None:
        config = MessagingConfig()
        config.weixin.group_enabled = True
        config.remote_control.quantclass_restart_enabled = True
        errors = config.validate()
        self.assertTrue(any("group" in error for error in errors))
        self.assertTrue(any("Quantclass" in error for error in errors))

    def test_channel_binding_is_exactly_one_matching_private_owner(self) -> None:
        config = MessagingConfig()
        config.telegram.allowed_user_ids = ["42", "43"]
        config.telegram.home_chat_id = "99"
        errors = config.validate()
        self.assertTrue(any("exactly one" in error for error in errors))
        self.assertTrue(any("same private chat" in error for error in errors))

    def test_remote_control_sentinel_requires_exact_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "REMOTE_CONTROL_ENABLED"
            self.assertFalse(remote_control_authorized(path)[0])
            set_remote_control_authorized(True, path)
            self.assertTrue(remote_control_authorized(path)[0])
            path.write_text("wrong\n", encoding="utf-8")
            self.assertFalse(remote_control_authorized(path)[0])
            set_remote_control_authorized(False, path)
            self.assertFalse(path.exists())

    def test_vault_never_writes_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            vault = CredentialVault(
                path,
                protect=lambda value: "protected:" + value[::-1],
                unprotect=lambda value: value.removeprefix("protected:")[::-1],
            )
            vault.set("telegram_bot_token", "123:very-secret")
            self.assertEqual(vault.get("telegram_bot_token"), "123:very-secret")
            self.assertNotIn("123:very-secret", path.read_text(encoding="utf-8"))
            self.assertIn("telegram_bot_token", vault.names())
            first = vault.ipc_auth_key()
            self.assertEqual(first, vault.ipc_auth_key())
            vault.delete("telegram_bot_token")
            self.assertFalse(vault.has("telegram_bot_token"))


if __name__ == "__main__":
    unittest.main()
