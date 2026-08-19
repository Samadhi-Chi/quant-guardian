from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from quant_guardian.gateway.config import (
    MessagingConfig,
    load_messaging_config,
    save_messaging_config,
    set_remote_control_authorized,
)
from quant_guardian.gateway.ipc import (
    GatewayIpcError,
    GuardianControlClient,
    GuardianControlServer,
    safe_status,
)
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore


class FakeAudit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event_type, payload, **kwargs):
        self.events.append((event_type, payload, kwargs))


class FakeStatus:
    def __init__(self, *, rocket: bool = False, manual_login: bool = False) -> None:
        self.rocket = {"active": rocket}
        self.probe = {"login_requires_manual": manual_login}

    def to_dict(self):
        return {
            "state": "healthy",
            "reason": "ok",
            "observed_at": "2026-08-19T10:00:00+08:00",
            "components": {
                "qmt_api": {"id": "qmt_api", "state": "healthy", "reason": "ok"},
                "trade_system": {
                    "id": "trade_system",
                    "state": "healthy",
                    "reason": "ok",
                },
            },
            "attention": {"message": "无需操作"},
            "schedule": {},
        }


class FakeService:
    def __init__(self) -> None:
        self.status = FakeStatus()
        self.audit = FakeAudit()
        self.restart_calls = []
        self.check_calls = []
        self._active_recovery = {}
        self.machine = SimpleNamespace(
            last_snapshot=SimpleNamespace(network_available=True)
        )
        self.check_hook = None
        self.events = []
        self.operations = []

    def operator_check(self, source, **kwargs):
        self.check_calls.append((source, kwargs))
        if self.check_hook is not None:
            self.check_hook()
        return self.status

    def manual_restart(self, **kwargs):
        self.restart_calls.append(kwargs)
        self._active_recovery = {"operation_id": "QGO-REMOTE-1"}
        return self.status

    def query_events(self, **_kwargs):
        return self.events

    def query_operations(self, **_kwargs):
        return self.operations


class GatewayIpcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config_path = root / "messaging.json"
        self.sentinel = root / "REMOTE_CONTROL_ENABLED"
        self.store = GatewayStore(root / "gateway.db")
        self.vault = CredentialVault(
            root / "secrets.json",
            protect=lambda value: "x" + value,
            unprotect=lambda value: value[1:],
        )
        config = MessagingConfig(gateway_enabled=True)
        config.telegram.enabled = True
        config.telegram.allowed_user_ids = ["42"]
        config.telegram.home_chat_id = "42"
        config.remote_control.enabled = True
        save_messaging_config(config, self.config_path)
        set_remote_control_authorized(True, self.sentinel)
        self.service = FakeService()
        self.address = rf"\\.\pipe\qg-test-{uuid.uuid4().hex}"
        self.server = GuardianControlServer(
            self.service,
            messaging_path=self.config_path,
            vault=self.vault,
            store=self.store,
            address=self.address,
            sentinel_path=self.sentinel,
        )
        self.server.start()
        self.client = GuardianControlClient(
            vault=self.vault,
            address=self.address,
            timeout_seconds=5,
        )

    def tearDown(self) -> None:
        self.server.stop()
        self.temporary.cleanup()

    def request(self, action, **kwargs):
        return self.client.request(
            action,
            channel="telegram",
            sender_id="42",
            chat_id="42",
            **kwargs,
        )

    def test_status_and_check_use_authenticated_pipe(self) -> None:
        result = self.request("status")
        self.assertEqual(result["status"]["state"], "healthy")
        self.request("check", params={"source": "qmt"})
        self.assertEqual(self.service.check_calls[-1][1]["initiator"], "remote_telegram")

    def test_ping_and_invalid_envelopes_fail_closed(self) -> None:
        self.assertEqual(
            self.client.request(
                "ping",
                channel="local",
                sender_id="local",
                chat_id="local",
            )["protocol"],
            1,
        )
        with self.assertRaisesRegex(GatewayIpcError, "unsupported remote action"):
            self.request("shell")
        envelope = {
            "protocol": 1,
            "request_id": "QGR-REPLAY",
            "nonce": "nonce-replay-1234",
            "issued_at": datetime.now().astimezone().isoformat(),
            "action": "ping",
            "channel": "local",
            "sender_id": "local",
            "chat_id": "local",
            "params": {},
        }
        self.server._validate_envelope(envelope)
        with self.assertRaisesRegex(GatewayIpcError, "replayed"):
            self.server._validate_envelope(envelope)

    def test_remote_status_never_exposes_local_path_or_account_number(self) -> None:
        status = FakeStatus().to_dict()
        status["reason"] = r"failed at C:\Users\sheng\QMT\secret.log account=123456789012"
        sanitized = str(safe_status(status))
        self.assertNotIn(r"C:\Users\sheng", sanitized)
        self.assertNotIn("123456789012", sanitized)

    def test_wrong_principal_is_rejected(self) -> None:
        with self.assertRaises(GatewayIpcError):
            self.client.request(
                "status",
                channel="telegram",
                sender_id="99",
                chat_id="99",
            )

    def test_remote_query_permissions_and_audit_fail_closed(self) -> None:
        config = load_messaging_config(self.config_path)
        config.remote_control.allow_status = False
        save_messaging_config(config, self.config_path)
        with self.assertRaisesRegex(GatewayIpcError, "disabled"):
            self.request("status", request_id="QGR-DISABLED")
        result = self.store.command_result("QGR-DISABLED")
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(event[0] == "remote_command_result" for event in self.service.audit.events))

    def test_incidents_and_operations_are_bounded_and_sanitized(self) -> None:
        self.service.events = [
            {
                "time": f"2026-08-19T10:{index:02d}:00+08:00",
                "event_type": "probe_failed",
                "severity": "warning" if index else "info",
                "payload": {
                    "component_id": "qmt_api",
                    "reason": rf"C:\Users\private\QMT account={123456789012 + index}",
                },
            }
            for index in range(12)
        ]
        self.service.operations = [
            {
                "operation_id": "QGO-1",
                "started_at": "2026-08-19T10:00:00+08:00",
                "operation_type": "qmt_restart",
                "initiator": "manual",
                "status": "succeeded",
                "summary": r"C:\Users\private\QMT\log.txt",
            }
        ]
        incidents = self.request("incidents")["incidents"]
        operations = self.request("operations")["operations"]
        self.assertEqual(len(incidents), 8)
        self.assertNotIn("Users\\private", str(incidents))
        self.assertNotIn("Users\\private", str(operations))

    def test_restart_requires_challenge_and_revalidates_before_service_call(self) -> None:
        challenge = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )
        result = self.request(
            "confirm_restart_qmt",
            request_id=challenge.request_id,
            params={"challenge_id": challenge.challenge_id},
        )
        self.assertEqual(result["operation_id"], "QGO-REMOTE-1")
        self.assertEqual(len(self.service.restart_calls), 1)
        self.assertEqual(self.service.restart_calls[0]["initiator"], "remote_telegram")

    def test_rocket_guard_blocks_remote_restart(self) -> None:
        self.service.status = FakeStatus(rocket=True)
        challenge = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )
        with self.assertRaises(GatewayIpcError) as raised:
            self.request(
                "confirm_restart_qmt",
                request_id=challenge.request_id,
                params={"challenge_id": challenge.challenge_id},
            )
        self.assertIn("Rocket", str(raised.exception))
        self.assertFalse(self.service.restart_calls)

    def test_network_guard_blocks_remote_restart(self) -> None:
        self.service.machine.last_snapshot.network_available = False
        challenge = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )
        with self.assertRaises(GatewayIpcError) as raised:
            self.request(
                "confirm_restart_qmt",
                request_id=challenge.request_id,
                params={"challenge_id": challenge.challenge_id},
            )
        self.assertIn("网络", str(raised.exception))
        self.assertFalse(self.service.restart_calls)

    def test_missing_network_snapshot_and_manual_login_block_restart(self) -> None:
        for mode in ("missing_snapshot", "manual_login"):
            with self.subTest(mode=mode):
                self.service.machine.last_snapshot = SimpleNamespace(network_available=True)
                self.service.status = FakeStatus(manual_login=mode == "manual_login")
                if mode == "missing_snapshot":
                    self.service.machine.last_snapshot = None
                challenge = self.store.create_challenge(
                    channel="telegram",
                    sender_id="42",
                    chat_id="42",
                    action="restart_qmt",
                    ttl_seconds=60,
                    require_code=False,
                )
                with self.assertRaises(GatewayIpcError):
                    self.request(
                        "confirm_restart_qmt",
                        request_id=challenge.request_id,
                        params={"challenge_id": challenge.challenge_id},
                    )
        self.assertFalse(self.service.restart_calls)

    def test_missing_local_sentinel_blocks_remote_restart(self) -> None:
        set_remote_control_authorized(False, self.sentinel)
        challenge = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )
        with self.assertRaises(GatewayIpcError):
            self.request(
                "confirm_restart_qmt",
                request_id=challenge.request_id,
                params={"challenge_id": challenge.challenge_id},
            )
        self.assertFalse(self.service.restart_calls)

    def test_authorization_is_rechecked_after_read_only_preflight(self) -> None:
        challenge = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )
        self.service.check_hook = lambda: set_remote_control_authorized(False, self.sentinel)
        with self.assertRaises(GatewayIpcError):
            self.request(
                "confirm_restart_qmt",
                request_id=challenge.request_id,
                params={"challenge_id": challenge.challenge_id},
            )
        self.assertFalse(self.service.restart_calls)

    def test_config_is_rechecked_after_read_only_preflight(self) -> None:
        challenge = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )

        def disable_remote_control() -> None:
            config = load_messaging_config(self.config_path)
            config.remote_control.enabled = False
            save_messaging_config(config, self.config_path)

        self.service.check_hook = disable_remote_control
        with self.assertRaisesRegex(GatewayIpcError, "确认过程中关闭"):
            self.request(
                "confirm_restart_qmt",
                request_id=challenge.request_id,
                params={"challenge_id": challenge.challenge_id},
            )
        self.assertFalse(self.service.restart_calls)

    def test_terminal_restart_result_is_idempotent(self) -> None:
        challenge = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )
        self.store.record_command(
            request_id=challenge.request_id,
            channel="telegram",
            sender_id="42",
            chat_id="42",
            command="restart_qmt",
            status="succeeded",
            reason="already accepted",
            operation_id="QGO-OLD",
        )
        result = self.request(
            "confirm_restart_qmt",
            request_id=challenge.request_id,
            params={"challenge_id": challenge.challenge_id},
        )
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["operation_id"], "QGO-OLD")
        self.assertFalse(self.service.restart_calls)

    def test_restart_hourly_limit_counts_completed_requests_not_pending_confirmation(self) -> None:
        for index in range(2):
            challenge = self.store.create_challenge(
                channel="telegram",
                sender_id="42",
                chat_id="42",
                action="restart_qmt",
                ttl_seconds=60,
                require_code=False,
            )
            self.store.record_command(
                request_id=challenge.request_id,
                channel="telegram",
                sender_id="42",
                chat_id="42",
                command="restart_qmt",
                status="awaiting_confirmation",
                completed=False,
            )
            self.request(
                "confirm_restart_qmt",
                request_id=challenge.request_id,
                params={"challenge_id": challenge.challenge_id},
            )
            self.assertEqual(len(self.service.restart_calls), index + 1)

        third = self.store.create_challenge(
            channel="telegram",
            sender_id="42",
            chat_id="42",
            action="restart_qmt",
            ttl_seconds=60,
            require_code=False,
        )
        with self.assertRaisesRegex(GatewayIpcError, "hourly limit"):
            self.request(
                "confirm_restart_qmt",
                request_id=third.request_id,
                params={"challenge_id": third.challenge_id},
            )


if __name__ == "__main__":
    unittest.main()
