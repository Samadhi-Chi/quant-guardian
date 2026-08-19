from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

from quant_guardian.config import app_data_dir
from quant_guardian.gateway.config import (
    MessagingConfig,
    default_messaging_config_path,
    load_messaging_config,
    remote_control_authorized,
)
from quant_guardian.gateway.privacy import safe_message_text
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65_536
MAX_CLOCK_SKEW_SECONDS = 90


class GatewayIpcError(RuntimeError):
    pass


def default_pipe_address() -> str:
    digest = hashlib.sha256(str(app_data_dir()).casefold().encode("utf-8")).hexdigest()[:12]
    if os.name == "nt":
        return rf"\\.\pipe\quant-guardian-control-{digest}"
    return str(app_data_dir() / "state" / f"control-{digest}.sock")


def _family(address: str | tuple[str, int]) -> str | None:
    if isinstance(address, tuple):
        return "AF_INET"
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _read_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise GatewayIpcError("IPC message is too large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise GatewayIpcError("IPC message root must be an object")
    return value


def _safe_text(value: Any, limit: int = 240) -> str:
    return safe_message_text(value, limit)


def _safe_component(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    children = value.get("children")
    return {
        "id": _safe_text(value.get("id"), 80),
        "name": _safe_text(value.get("name"), 80),
        "state": _safe_text(value.get("state"), 32),
        "reason": _safe_text(value.get("reason")),
        "children": [
            _safe_component(item)
            for item in (children if isinstance(children, list) else [])
            if isinstance(item, dict)
        ],
    }


def safe_status(status: Any) -> dict[str, Any]:
    document = status.to_dict() if hasattr(status, "to_dict") else dict(status or {})
    components = document.get("components")
    safe_components = {
        str(key): _safe_component(value)
        for key, value in (components.items() if isinstance(components, dict) else [])
    }
    schedule = document.get("schedule") if isinstance(document.get("schedule"), dict) else {}
    attention = document.get("attention") if isinstance(document.get("attention"), dict) else {}
    return {
        "state": _safe_text(document.get("state"), 32),
        "reason": _safe_text(document.get("reason")),
        "observed_at": _safe_text(document.get("observed_at"), 64),
        "components": safe_components,
        "attention": {
            "title": _safe_text(attention.get("title"), 100),
            "message": _safe_text(attention.get("message")),
            "action": _safe_text(attention.get("action"), 80),
        },
        "schedule": {
            "mode": _safe_text(schedule.get("mode"), 32),
            "next_check_at": _safe_text(schedule.get("next_check_at"), 64),
            "interval_seconds": schedule.get("interval_seconds"),
        },
    }


class GuardianControlClient:
    def __init__(
        self,
        *,
        vault: CredentialVault | None = None,
        address: str | tuple[str, int] | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.vault = vault or CredentialVault()
        self.address = address or default_pipe_address()
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        action: str,
        *,
        channel: str,
        sender_id: str,
        chat_id: str,
        params: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        document = {
            "protocol": PROTOCOL_VERSION,
            "request_id": request_id or f"QGR-{secrets.token_hex(10)}",
            "nonce": secrets.token_urlsafe(18),
            "issued_at": datetime.now().astimezone().isoformat(),
            "action": action,
            "channel": channel,
            "sender_id": sender_id,
            "chat_id": chat_id,
            "params": params or {},
        }
        connection = None
        try:
            connection = Client(
                self.address,
                family=_family(self.address),
                authkey=self.vault.ipc_auth_key(),
            )
            connection.send_bytes(_json_bytes(document))
            if not connection.poll(self.timeout_seconds):
                raise GatewayIpcError("Guardian IPC response timed out")
            response = _read_json(connection.recv_bytes(MAX_MESSAGE_BYTES))
        except (OSError, EOFError) as exc:
            raise GatewayIpcError(f"Guardian IPC unavailable: {type(exc).__name__}") from exc
        finally:
            if connection is not None:
                connection.close()
        if not response.get("ok"):
            raise GatewayIpcError(_safe_text(response.get("error") or "Guardian rejected request"))
        return dict(response.get("result") or {})


class GuardianControlServer:
    """Authenticated local command broker owned by the Guardian process."""

    def __init__(
        self,
        service: Any,
        *,
        messaging_path: Path | None = None,
        vault: CredentialVault | None = None,
        store: GatewayStore | None = None,
        address: str | tuple[str, int] | None = None,
        sentinel_path: Path | None = None,
    ) -> None:
        self.service = service
        self.messaging_path = messaging_path or default_messaging_config_path()
        self.vault = vault or CredentialVault()
        self.store = store or GatewayStore()
        self.address = address or default_pipe_address()
        self.sentinel_path = sentinel_path
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: Listener | None = None
        self._startup_error = ""
        self._seen_nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._startup_error = ""
        self._thread = threading.Thread(
            target=self._serve,
            name="quant-guardian-control-pipe",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Guardian control pipe did not start")
        if self._startup_error or not self.running:
            raise RuntimeError(
                self._startup_error or "Guardian control pipe stopped during startup"
            )

    def stop(self) -> None:
        self._stop.set()
        try:
            client = Client(
                self.address,
                family=_family(self.address),
                authkey=self.vault.ipc_auth_key(),
            )
            client.send_bytes(
                _json_bytes(
                    {
                        "protocol": PROTOCOL_VERSION,
                        "request_id": "shutdown-probe",
                        "nonce": secrets.token_urlsafe(12),
                        "issued_at": datetime.now().astimezone().isoformat(),
                        "action": "ping",
                        "channel": "local",
                        "sender_id": "local",
                        "chat_id": "local",
                        "params": {},
                    }
                )
            )
            client.close()
        except (OSError, EOFError):
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _serve(self) -> None:
        try:
            self._listener = Listener(
                self.address,
                family=_family(self.address),
                authkey=self.vault.ipc_auth_key(),
            )
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        self._ready.set()
        while not self._stop.is_set():
            connection = None
            try:
                connection = self._listener.accept()
                request = _read_json(connection.recv_bytes(MAX_MESSAGE_BYTES))
                response = self._handle(request)
                connection.send_bytes(_json_bytes(response))
            except (OSError, EOFError):
                if self._stop.is_set():
                    break
            except Exception as exc:  # fail closed at the trust boundary
                if connection is not None:
                    try:
                        connection.send_bytes(
                            _json_bytes(
                                {
                                    "ok": False,
                                    "error": f"Guardian command broker error: {type(exc).__name__}",
                                }
                            )
                        )
                    except (OSError, EOFError):
                        pass
            finally:
                if connection is not None:
                    connection.close()
        if self._listener is not None:
            self._listener.close()

    def _validate_envelope(self, request: dict[str, Any]) -> None:
        if int(request.get("protocol", 0)) != PROTOCOL_VERSION:
            raise GatewayIpcError("unsupported IPC protocol")
        action = str(request.get("action") or "")
        if action not in {
            "ping",
            "status",
            "check",
            "incidents",
            "operations",
            "confirm_restart_qmt",
        }:
            raise GatewayIpcError("unsupported remote action")
        issued_raw = str(request.get("issued_at") or "")
        try:
            issued = datetime.fromisoformat(issued_raw)
        except ValueError as exc:
            raise GatewayIpcError("invalid request timestamp") from exc
        if issued.tzinfo is None:
            raise GatewayIpcError("request timestamp must include timezone")
        skew = abs((datetime.now().astimezone() - issued).total_seconds())
        if skew > MAX_CLOCK_SKEW_SECONDS:
            raise GatewayIpcError("request timestamp is outside the allowed window")
        nonce = str(request.get("nonce") or "")
        if len(nonce) < 12:
            raise GatewayIpcError("request nonce is invalid")
        now = time.monotonic()
        with self._nonce_lock:
            self._seen_nonces = {
                key: value
                for key, value in self._seen_nonces.items()
                if now - value <= MAX_CLOCK_SKEW_SECONDS * 2
            }
            if nonce in self._seen_nonces:
                raise GatewayIpcError("replayed request")
            self._seen_nonces[nonce] = now

    @staticmethod
    def _channel_config(config: MessagingConfig, channel: str) -> Any:
        if channel == "telegram":
            return config.telegram
        if channel == "weixin":
            return config.weixin
        return None

    def _validate_principal(
        self,
        config: MessagingConfig,
        *,
        channel: str,
        sender_id: str,
        chat_id: str,
    ) -> None:
        channel_config = self._channel_config(config, channel)
        if channel_config is None or not channel_config.enabled:
            raise GatewayIpcError("message channel is not enabled")
        allowed = tuple(str(value) for value in channel_config.allowed_user_ids)
        if not any(hmac.compare_digest(sender_id, value) for value in allowed):
            raise GatewayIpcError("remote sender is not authorized")
        home_chat = str(channel_config.home_chat_id or "")
        if home_chat and not hmac.compare_digest(chat_id, home_chat):
            raise GatewayIpcError("remote chat is not the bound private chat")

    def _rate_limit(
        self,
        config: MessagingConfig,
        *,
        channel: str,
        sender_id: str,
        action: str,
    ) -> None:
        total = self.store.count_recent_commands(
            channel=channel,
            sender_id=sender_id,
            seconds=60,
        )
        if total >= config.remote_control.max_commands_per_minute:
            raise GatewayIpcError("remote command rate limit exceeded")
        if action == "confirm_restart_qmt":
            restarts = self.store.count_recent_commands(
                channel=channel,
                sender_id=sender_id,
                command="restart_qmt",
                seconds=3600,
                terminal_only=True,
            )
            if restarts >= config.remote_control.max_restart_requests_per_hour:
                raise GatewayIpcError("remote QMT restart hourly limit exceeded")

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "")
        channel = str(request.get("channel") or "")
        sender_id = str(request.get("sender_id") or "")
        chat_id = str(request.get("chat_id") or "")
        request_id = str(request.get("request_id") or "")
        try:
            self._validate_envelope(request)
            if action == "ping":
                return {"ok": True, "result": {"protocol": PROTOCOL_VERSION}}
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            config = load_messaging_config(self.messaging_path)
            self._validate_principal(
                config,
                channel=channel,
                sender_id=sender_id,
                chat_id=chat_id,
            )
            self._rate_limit(
                config,
                channel=channel,
                sender_id=sender_id,
                action=action,
            )
            result = self._dispatch(
                action,
                config=config,
                channel=channel,
                sender_id=sender_id,
                chat_id=chat_id,
                request_id=request_id,
                params=params,
            )
            return {"ok": True, "result": result}
        except (GatewayIpcError, PermissionError, RuntimeError, ValueError) as exc:
            if action != "ping" and request_id and channel and sender_id:
                command = (
                    "restart_qmt" if action == "confirm_restart_qmt" else action or "unknown"
                )
                try:
                    self.store.record_command(
                        request_id=request_id,
                        channel=channel,
                        sender_id=sender_id,
                        chat_id=chat_id,
                        command=command,
                        status="blocked",
                        reason=_safe_text(exc),
                    )
                    self._record_command_audit(
                        request_id,
                        channel,
                        command,
                        "blocked",
                        _safe_text(exc),
                        target="qmt_api" if command == "restart_qmt" else "quant_guardian.messaging",
                    )
                except Exception:
                    pass
            return {"ok": False, "error": _safe_text(exc)}

    def _dispatch(
        self,
        action: str,
        *,
        config: MessagingConfig,
        channel: str,
        sender_id: str,
        chat_id: str,
        request_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        remote = config.remote_control
        if action == "status":
            if not remote.allow_status:
                raise PermissionError("remote status query is disabled")
            result = {"status": safe_status(self.service.status)}
            self._record_command_audit(
                request_id, channel, "status", "succeeded", "remote status returned"
            )
            return result
        if action == "check":
            if not remote.allow_check:
                raise PermissionError("remote health check is disabled")
            source = str(params.get("source") or "all")
            status = self.service.operator_check(
                source,
                initiator=f"remote_{channel}",
                remote_channel=channel,
                remote_request_id=request_id,
            )
            self._record_command_audit(
                request_id, channel, "check", "succeeded", "remote check completed"
            )
            return {"status": safe_status(status)}
        if action == "incidents":
            if not remote.allow_incidents:
                raise PermissionError("remote incident query is disabled")
            since = datetime.now().astimezone() - timedelta(days=1)
            events = self.service.query_events(limit=20, severity="all", since=since)
            values = []
            for item in events:
                severity = str(item.get("severity") or "info")
                if severity not in {"warning", "critical"}:
                    continue
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                values.append(
                    {
                        "time": _safe_text(item.get("time"), 64),
                        "event_type": _safe_text(item.get("event_type"), 80),
                        "severity": severity,
                        "component_id": _safe_text(
                            item.get("component_id") or payload.get("component_id"), 80
                        ),
                        "summary": _safe_text(
                            item.get("summary") or payload.get("reason") or "监控异常"
                        ),
                    }
                )
                if len(values) >= 8:
                    break
            self._record_command_audit(
                request_id, channel, "incidents", "succeeded", "remote incidents returned"
            )
            return {"incidents": values}
        if action == "operations":
            if not remote.allow_operations:
                raise PermissionError("remote operation query is disabled")
            operations = self.service.query_operations(limit=10)
            values = [
                {
                    "operation_id": _safe_text(item.get("operation_id"), 80),
                    "started_at": _safe_text(item.get("started_at"), 64),
                    "operation_type": _safe_text(item.get("operation_type"), 60),
                    "initiator": _safe_text(item.get("initiator"), 40),
                    "status": _safe_text(item.get("status"), 40),
                    "summary": _safe_text(item.get("summary")),
                }
                for item in operations
            ]
            self._record_command_audit(
                request_id, channel, "operations", "succeeded", "remote operations returned"
            )
            return {"operations": values}
        if action != "confirm_restart_qmt":
            raise GatewayIpcError("unsupported action")
        if not remote.enabled or not remote.qmt_restart_enabled:
            raise PermissionError("remote QMT restart is disabled")
        authorized, reason = remote_control_authorized(self.sentinel_path)
        if not authorized:
            raise PermissionError(reason)
        challenge_id = str(params.get("challenge_id") or "")
        code = str(params.get("code") or "")
        pending = self.store.find_challenge(
            challenge_id=challenge_id,
            channel=channel,
            sender_id=sender_id,
        )
        if pending is not None:
            previous = self.store.command_result(pending.request_id)
            if previous and previous.get("status") in {"succeeded", "failed", "blocked"}:
                return {
                    "status": str(previous.get("status")),
                    "reason": _safe_text(previous.get("reason")),
                    "operation_id": str(previous.get("operation_id") or ""),
                    "idempotent": True,
                }
        challenge, reason = self.store.consume_challenge(
            challenge_id=challenge_id,
            channel=channel,
            sender_id=sender_id,
            code=code,
        )
        if challenge is None or challenge.action != "restart_qmt":
            raise PermissionError(reason if challenge is None else "confirmation action mismatch")
        current = self.service.operator_check(
            "qmt",
            initiator=f"remote_{channel}",
            remote_channel=channel,
            remote_request_id=challenge.request_id,
        )
        machine = getattr(self.service, "machine", None)
        snapshot = getattr(machine, "last_snapshot", None)
        if snapshot is None:
            raise PermissionError("无法确认最新网络状态，远程QMT重启被安全闸门阻断")
        if not bool(getattr(snapshot, "network_available", False)):
            raise PermissionError("本机网络不可用，远程QMT重启被安全闸门阻断")
        rocket = getattr(current, "rocket", {}) or {}
        if bool(rocket.get("active")):
            raise PermissionError("Rocket正在运行，远程QMT重启被安全闸门阻断")
        probe = getattr(current, "probe", {}) or {}
        if bool(probe.get("login_requires_manual")):
            raise PermissionError("QMT需要人工登录，远程重启不会继续")
        fresh_config = load_messaging_config(self.messaging_path)
        self._validate_principal(
            fresh_config,
            channel=channel,
            sender_id=sender_id,
            chat_id=chat_id,
        )
        if (
            not fresh_config.remote_control.enabled
            or not fresh_config.remote_control.qmt_restart_enabled
        ):
            raise PermissionError("远程QMT重启授权已在确认过程中关闭")
        authorized, reason = remote_control_authorized(self.sentinel_path)
        if not authorized:
            raise PermissionError(reason)
        status = self.service.manual_restart(
            operator_confirmed=True,
            initiator=f"remote_{channel}",
            remote_channel=channel,
            remote_request_id=challenge.request_id,
        )
        active = getattr(self.service, "_active_recovery", None) or {}
        operation_id = str(active.get("operation_id") or "")
        self.store.record_command(
            request_id=challenge.request_id,
            channel=channel,
            sender_id=sender_id,
            chat_id=chat_id,
            command="restart_qmt",
            status="succeeded",
            reason="QMT controlled restart was accepted and launched",
            operation_id=operation_id,
        )
        self._record_command_audit(
            challenge.request_id,
            channel,
            "restart_qmt",
            "succeeded",
            "remote QMT restart accepted",
            target="qmt_api",
            operation_id=operation_id,
        )
        return {
            "status": "accepted",
            "reason": "QMT受控重启已启动，Guardian正在验证恢复结果",
            "operation_id": operation_id,
            "guardian": safe_status(status),
        }

    def _record_command_audit(
        self,
        request_id: str,
        channel: str,
        command: str,
        status: str,
        reason: str,
        *,
        target: str = "quant_guardian.messaging",
        operation_id: str = "",
    ) -> None:
        audit = getattr(self.service, "audit", None)
        if audit is None:
            return
        at = datetime.now().astimezone()
        identifier = request_id or f"QGRC-{secrets.token_hex(8)}"
        audit.record(
            "remote_command_result",
            {
                "component_id": "quant_guardian.messaging",
                "operation_id": identifier,
                "linked_operation_id": operation_id,
                "operation_type": "remote_command",
                "initiator": f"remote_{channel}",
                "target_component": target,
                "context": "production",
                "command": command,
                "remote_channel": channel,
                "started_at": at,
                "completed_at": at,
                "status": status,
                "phase": "completed",
                "reason": reason,
            },
            severity="warning" if status in {"failed", "blocked"} else "info",
            moment=at,
            event_id=identifier,
        )
