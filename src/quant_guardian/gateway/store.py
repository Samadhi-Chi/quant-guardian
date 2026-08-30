from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quant_guardian.gateway.config import gateway_database_path
from quant_guardian.gateway.models import Challenge, OutboundMessage


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now().astimezone()).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@contextmanager
def closing(connection: sqlite3.Connection):
    """Commit or roll back the transaction, then always release the file handle."""

    try:
        with connection:
            yield connection
    finally:
        connection.close()


class GatewayStore:
    """Durable gateway state, queue, deduplication, and security audit index."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or gateway_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_state(
                    channel TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    identity TEXT NOT NULL DEFAULT '',
                    last_connected_at TEXT NOT NULL DEFAULT '',
                    last_received_at TEXT NOT NULL DEFAULT '',
                    last_sent_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    reconnect_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbound(
                    message_key TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    text_hash TEXT NOT NULL DEFAULT '',
                    command TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT 'received'
                );
                CREATE INDEX IF NOT EXISTS ix_inbound_received
                    ON inbound(received_at DESC);
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    buttons_json TEXT NOT NULL DEFAULT '[]',
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS ix_outbox_pending
                    ON outbox(status,next_attempt_at,priority DESC,id);
                CREATE TABLE IF NOT EXISTS challenges(
                    challenge_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    channel TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    params_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS ix_challenges_lookup
                    ON challenges(channel,sender_id,status,expires_at);
                CREATE TABLE IF NOT EXISTS command_log(
                    request_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    operation_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS ix_command_created
                    ON command_log(created_at DESC,channel,status);
                """
            )
            connection.execute(
                "UPDATE outbox SET status='pending', next_attempt_at=? WHERE status='delivering'",
                (_iso(),),
            )

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO meta(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, _iso()),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def update_channel_state(
        self,
        channel: str,
        status: str,
        *,
        identity: str = "",
        received: bool = False,
        sent: bool = False,
        error: str = "",
        reconnected: bool = False,
    ) -> None:
        now = _iso()
        with self._lock, closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM channel_state WHERE channel=?", (channel,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO channel_state(
                    channel,status,identity,last_connected_at,last_received_at,
                    last_sent_at,last_error,reconnect_count,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel) DO UPDATE SET
                    status=excluded.status,
                    identity=CASE WHEN excluded.identity<>'' THEN excluded.identity ELSE channel_state.identity END,
                    last_connected_at=CASE WHEN excluded.last_connected_at<>'' THEN excluded.last_connected_at ELSE channel_state.last_connected_at END,
                    last_received_at=CASE WHEN excluded.last_received_at<>'' THEN excluded.last_received_at ELSE channel_state.last_received_at END,
                    last_sent_at=CASE WHEN excluded.last_sent_at<>'' THEN excluded.last_sent_at ELSE channel_state.last_sent_at END,
                    last_error=excluded.last_error,
                    reconnect_count=excluded.reconnect_count,
                    updated_at=excluded.updated_at
                """,
                (
                    channel,
                    status,
                    identity,
                    now if status == "connected" else "",
                    now if received else "",
                    now if sent else "",
                    error[:500],
                    int(existing[7] if existing else 0) + (1 if reconnected else 0),
                    now,
                ),
            )

    def channel_states(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM channel_state ORDER BY channel").fetchall()
        return [dict(row) for row in rows]

    def record_inbound_once(
        self,
        *,
        message_key: str,
        channel: str,
        sender_id: str,
        chat_id: str,
        text_hash: str = "",
    ) -> bool:
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO inbound(message_key,channel,sender_id,chat_id,received_at,text_hash) "
                "VALUES(?,?,?,?,?,?)",
                (message_key, channel, sender_id, chat_id, _iso(), text_hash),
            )
            return cursor.rowcount == 1

    def finish_inbound(self, message_key: str, command: str, outcome: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE inbound SET command=?,outcome=? WHERE message_key=?",
                (command, outcome, message_key),
            )

    def enqueue_outbound(
        self,
        *,
        channel: str,
        chat_id: str,
        text: str,
        idempotency_key: str,
        buttons: tuple[tuple[tuple[str, str], ...], ...] = (),
        priority: int = 0,
    ) -> bool:
        if not text.strip() or not channel or not chat_id or not idempotency_key:
            return False
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO outbox(channel,chat_id,text,buttons_json,priority,status,attempts,next_attempt_at,created_at,idempotency_key) "
                "VALUES(?,?,?,?,?,'pending',0,?,?,?)",
                (
                    channel,
                    chat_id,
                    text,
                    json.dumps(buttons, ensure_ascii=False),
                    int(priority),
                    _iso(),
                    _iso(),
                    idempotency_key,
                ),
            )
            return cursor.rowcount == 1

    def claim_outbound(self, channel: str, limit: int = 20) -> list[OutboundMessage]:
        now = _iso()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM outbox WHERE channel=? AND status='pending' AND next_attempt_at<=? "
                "ORDER BY priority DESC,id LIMIT ?",
                (channel, now, max(1, min(int(limit), 100))),
            ).fetchall()
            if rows:
                connection.executemany(
                    "UPDATE outbox SET status='delivering',attempts=attempts+1 WHERE id=?",
                    [(row["id"],) for row in rows],
                )
            connection.commit()
        values: list[OutboundMessage] = []
        for row in rows:
            raw_buttons = json.loads(row["buttons_json"] or "[]")
            buttons = tuple(
                tuple((str(item[0]), str(item[1])) for item in group) for group in raw_buttons
            )
            values.append(
                OutboundMessage(
                    message_id=int(row["id"]),
                    channel=str(row["channel"]),
                    chat_id=str(row["chat_id"]),
                    text=str(row["text"]),
                    buttons=buttons,
                    idempotency_key=str(row["idempotency_key"]),
                    attempts=int(row["attempts"]) + 1,
                )
            )
        return values

    def complete_outbound(
        self,
        message_id: int,
        *,
        success: bool,
        error: str = "",
        retry_seconds: int = 0,
    ) -> None:
        now = datetime.now().astimezone()
        status = "sent" if success else "pending" if retry_seconds else "failed"
        next_at = now + timedelta(seconds=max(0, retry_seconds))
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE outbox SET status=?,sent_at=?,error=?,next_attempt_at=? WHERE id=?",
                (
                    status,
                    _iso(now) if success else "",
                    error[:500],
                    _iso(next_at),
                    int(message_id),
                ),
            )

    def create_challenge(
        self,
        *,
        channel: str,
        sender_id: str,
        chat_id: str,
        action: str,
        ttl_seconds: int,
        require_code: bool,
        params: dict[str, Any] | None = None,
    ) -> Challenge:
        now = datetime.now().astimezone()
        challenge_id = f"QGC-{secrets.token_hex(12)}"
        request_id = f"QGR-{now:%Y%m%d}-{secrets.token_hex(6)}"
        code = f"QG-{secrets.randbelow(10000):04d}" if require_code else ""
        expires = now + timedelta(seconds=ttl_seconds)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE challenges SET status='expired' WHERE status='pending' AND expires_at<=?",
                (_iso(now),),
            )
            connection.execute(
                "INSERT INTO challenges(challenge_id,request_id,channel,sender_id,chat_id,action,code,params_json,created_at,expires_at,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'pending')",
                (
                    challenge_id,
                    request_id,
                    channel,
                    sender_id,
                    chat_id,
                    action,
                    code,
                    json.dumps(params or {}, ensure_ascii=False),
                    _iso(now),
                    _iso(expires),
                ),
            )
        return Challenge(
            challenge_id,
            request_id,
            channel,
            sender_id,
            chat_id,
            action,
            code,
            now,
            expires,
            "pending",
            params or {},
        )

    @staticmethod
    def _challenge_from_row(row: sqlite3.Row) -> Challenge:
        return Challenge(
            challenge_id=str(row["challenge_id"]),
            request_id=str(row["request_id"]),
            channel=str(row["channel"]),
            sender_id=str(row["sender_id"]),
            chat_id=str(row["chat_id"]),
            action=str(row["action"]),
            code=str(row["code"]),
            created_at=_dt(str(row["created_at"])),
            expires_at=_dt(str(row["expires_at"])),
            status=str(row["status"]),
            params=json.loads(row["params_json"] or "{}"),
        )

    def find_challenge(
        self,
        *,
        channel: str,
        sender_id: str,
        challenge_id: str = "",
        code: str = "",
    ) -> Challenge | None:
        now = _iso()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE challenges SET status='expired' WHERE status='pending' AND expires_at<=?",
                (now,),
            )
            if challenge_id:
                row = connection.execute(
                    "SELECT * FROM challenges WHERE challenge_id=? AND channel=? AND sender_id=?",
                    (challenge_id, channel, sender_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM challenges WHERE code=? AND channel=? AND sender_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (code, channel, sender_id),
                ).fetchone()
        return self._challenge_from_row(row) if row else None

    def create_pairing(self, *, channel: str, ttl_seconds: int) -> Challenge:
        """Create a short-lived pairing code before the remote sender is known."""

        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE challenges SET status='cancelled',consumed_at=? "
                "WHERE channel=? AND action='pair_channel' AND status='pending'",
                (_iso(), channel),
            )
        challenge = self.create_challenge(
            channel=channel,
            sender_id="",
            chat_id="",
            action="pair_channel",
            ttl_seconds=ttl_seconds,
            require_code=False,
        )
        raw = secrets.token_hex(4).upper()
        code = f"QGP-{raw[:4]}-{raw[4:]}"
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE challenges SET code=? WHERE challenge_id=?",
                (code, challenge.challenge_id),
            )
        return Challenge(
            challenge.challenge_id,
            challenge.request_id,
            challenge.channel,
            challenge.sender_id,
            challenge.chat_id,
            challenge.action,
            code,
            challenge.created_at,
            challenge.expires_at,
            challenge.status,
            challenge.params,
        )

    def consume_pairing(
        self,
        *,
        channel: str,
        sender_id: str,
        chat_id: str,
        code: str,
    ) -> tuple[Challenge | None, str]:
        now = datetime.now().astimezone()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM challenges WHERE channel=? AND action='pair_channel' "
                "AND code=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                (channel, code.strip().upper()),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None, "配对码无效"
            challenge = self._challenge_from_row(row)
            if challenge.expires_at <= now:
                connection.execute(
                    "UPDATE challenges SET status='expired' WHERE challenge_id=?",
                    (challenge.challenge_id,),
                )
                connection.commit()
                return None, "配对码已过期"
            connection.execute(
                "UPDATE challenges SET status='consumed',consumed_at=?,sender_id=?,chat_id=? "
                "WHERE challenge_id=?",
                (_iso(now), sender_id, chat_id, challenge.challenge_id),
            )
            connection.commit()
            return challenge, "配对成功"

    def consume_challenge(
        self,
        *,
        challenge_id: str,
        channel: str,
        sender_id: str,
        code: str = "",
    ) -> tuple[Challenge | None, str]:
        now = datetime.now().astimezone()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None, "确认请求不存在"
            challenge = self._challenge_from_row(row)
            if challenge.channel != channel or challenge.sender_id != sender_id:
                connection.rollback()
                return None, "确认身份与原请求不一致"
            if challenge.status != "pending":
                connection.rollback()
                return None, "确认请求已使用或已失效"
            if challenge.expires_at <= now:
                connection.execute(
                    "UPDATE challenges SET status='expired' WHERE challenge_id=?",
                    (challenge_id,),
                )
                connection.commit()
                return None, "确认请求已过期"
            if challenge.code and challenge.code.casefold() != code.strip().casefold():
                connection.rollback()
                return None, "确认码不匹配"
            connection.execute(
                "UPDATE challenges SET status='consumed',consumed_at=? WHERE challenge_id=?",
                (_iso(now), challenge_id),
            )
            connection.commit()
            return challenge, "确认有效"

    def cancel_challenge(self, challenge_id: str, *, channel: str, sender_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE challenges SET status='cancelled',consumed_at=? "
                "WHERE challenge_id=? AND channel=? AND sender_id=? AND status='pending'",
                (_iso(), challenge_id, channel, sender_id),
            )
            return cursor.rowcount == 1

    def count_recent_commands(
        self,
        *,
        channel: str,
        sender_id: str,
        command: str | None = None,
        seconds: int,
        terminal_only: bool = False,
    ) -> int:
        cutoff = datetime.now().astimezone() - timedelta(seconds=seconds)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM command_log WHERE channel=? AND sender_id=? "
                "AND created_at>=? AND (? IS NULL OR command=?) "
                "AND (?=0 OR status IN ('succeeded','failed','blocked'))",
                (
                    channel,
                    sender_id,
                    _iso(cutoff),
                    command,
                    command,
                    1 if terminal_only else 0,
                ),
            ).fetchone()
        return int(row[0] if row else 0)

    def record_command(
        self,
        *,
        request_id: str,
        channel: str,
        sender_id: str,
        chat_id: str,
        command: str,
        status: str,
        reason: str = "",
        operation_id: str = "",
        completed: bool = True,
    ) -> None:
        now = _iso()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO command_log(
                    request_id,channel,sender_id,chat_id,command,status,reason,
                    operation_id,created_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status=excluded.status,reason=excluded.reason,
                    operation_id=excluded.operation_id,completed_at=excluded.completed_at
                """,
                (
                    request_id,
                    channel,
                    sender_id,
                    chat_id,
                    command,
                    status,
                    reason[:1000],
                    operation_id,
                    now,
                    now if completed else "",
                ),
            )

    def command_result(self, request_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM command_log WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else {}

    def activity(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT created_at AS time,channel,'command' AS kind,command AS action,
                       status,reason,request_id AS item_id
                FROM command_log
                UNION ALL
                SELECT created_at AS time,channel,'delivery' AS kind,'message' AS action,
                       status,error AS reason,CAST(id AS TEXT) AS item_id
                FROM outbox
                ORDER BY time DESC LIMIT ?
                """,
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self, *, since: datetime | None = None) -> dict[str, Any]:
        cutoff = _iso(since or (datetime.now().astimezone() - timedelta(days=1)))
        with closing(self._connect()) as connection:
            deliveries = connection.execute(
                "SELECT status,COUNT(*) AS count FROM outbox WHERE created_at>=? GROUP BY status",
                (cutoff,),
            ).fetchall()
            commands = connection.execute(
                "SELECT command,status,channel,COUNT(*) AS count FROM command_log "
                "WHERE created_at>=? GROUP BY command,status,channel",
                (cutoff,),
            ).fetchall()
        delivery_counts = {str(row["status"]): int(row["count"]) for row in deliveries}
        total = sum(delivery_counts.values())
        sent = delivery_counts.get("sent", 0)
        return {
            "deliveries_total": total,
            "deliveries_sent": sent,
            "delivery_success_rate": sent / total if total else 0.0,
            "deliveries_failed": delivery_counts.get("failed", 0),
            "deliveries_pending": delivery_counts.get("pending", 0)
            + delivery_counts.get("delivering", 0),
            "commands_total": sum(int(row["count"]) for row in commands),
            "remote_restarts": sum(
                int(row["count"])
                for row in commands
                if row["command"] == "restart_qmt" and row["status"] == "succeeded"
            ),
            "commands_by_channel": {
                channel: sum(int(row["count"]) for row in commands if row["channel"] == channel)
                for channel in {str(row["channel"]) for row in commands}
            },
        }
