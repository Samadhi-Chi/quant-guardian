from __future__ import annotations

import json
import queue
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _summary(document: dict[str, Any]) -> str:
    payload = document.get("payload")
    value = payload if isinstance(payload, dict) else {}
    result = (
        document.get("summary")
        or value.get("summary")
        or value.get("reason")
        or value.get("error")
        or value.get("new_state")
    )
    if result is None and "success" in value:
        result = "成功" if value.get("success") else "失败"
    return str(result or "")[:500]


def _timestamp(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return datetime.now().astimezone().timestamp()


class MonitoringStore:
    """SQLite query index for audit events and persistent health samples.

    JSONL remains the canonical audit trail.  SQLite is deliberately treated as
    rebuildable cache, so any database failure must never stop monitoring or
    recovery decisions.
    """

    def __init__(
        self,
        path: Path,
        *,
        audit_directory: Path,
        retention_days: int = 30,
        enabled: bool = True,
    ) -> None:
        self.path = path
        self.audit_directory = audit_directory
        self.retention_days = retention_days
        self.enabled = enabled
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue(maxsize=32768)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._thread = threading.Thread(
                target=self._worker,
                name="quant-guardian-store",
                daemon=True,
            )
            self._thread.start()
            self._ready.wait(timeout=2)

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=1000")
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if existing and "event_id TEXT PRIMARY KEY" in str(existing[0]):
            connection.execute("ALTER TABLE events RENAME TO events_v1")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                time TEXT NOT NULL,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                component_id TEXT NOT NULL DEFAULT '',
                subcomponent_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL
            )
            """
        )
        if existing and "event_id TEXT PRIMARY KEY" in str(existing[0]):
            connection.execute(
                "INSERT INTO events(event_id,time,timestamp,event_type,severity,"
                "component_id,subcomponent_id,summary,payload_json) "
                "SELECT event_id,time,timestamp,event_type,severity,component_id,"
                "subcomponent_id,summary,payload_json FROM events_v1"
            )
            connection.execute("DROP TABLE events_v1")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_identity "
            "ON events(event_id,time,event_type)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_filter "
            "ON events(severity, component_id, timestamp DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS health_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                timestamp REAL NOT NULL,
                state TEXT NOT NULL,
                action TEXT NOT NULL,
                qmt_state TEXT NOT NULL,
                trade_state TEXT NOT NULL,
                latency_ms REAL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_time "
            "ON health_samples(timestamp DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                started_timestamp REAL NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                completed_timestamp REAL,
                operation_type TEXT NOT NULL,
                initiator TEXT NOT NULL,
                target_component TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT 'production',
                status TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT '',
                attempt_no INTEGER NOT NULL DEFAULT 1,
                duration_ms REAL,
                summary TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_time "
            "ON operations(started_timestamp DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_filter "
            "ON operations(operation_type, status, initiator, started_timestamp DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_incident "
            "ON operations(incident_id, started_timestamp ASC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                detected_at TEXT NOT NULL,
                detected_timestamp REAL NOT NULL,
                resolved_at TEXT NOT NULL DEFAULT '',
                resolved_timestamp REAL,
                component_id TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT 'production',
                status TEXT NOT NULL,
                result TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                duration_ms REAL,
                summary TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_incidents_time "
            "ON incidents(detected_timestamp DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_incidents_filter "
            "ON incidents(status, component_id, detected_timestamp DESC)"
        )
        connection.commit()

    def _worker(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=2)
            self._configure(connection)
            self._ready.set()
            count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if count == 0:
                self._reindex(connection)
            operation_count = connection.execute(
                "SELECT COUNT(*) FROM operations"
            ).fetchone()[0]
            if operation_count == 0 and count:
                self._rebuild_operations(connection)
            elif operation_count and count == 0:
                # A fresh SQLite cache may be populated from legacy JSONL.
                # Those records predate explicit terminal verification events,
                # so normalize them after the initial reindex as well.
                self._finalize_legacy_recoveries(connection)
                connection.commit()
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                batch = [item]
                stop_after_batch = False
                for _index in range(255):
                    try:
                        pending = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if pending is None:
                        stop_after_batch = True
                        break
                    batch.append(pending)
                for operation, value in batch:
                    try:
                        if operation == "event":
                            self._insert_event(connection, value)
                        elif operation == "sample":
                            self._insert_sample(connection, value)
                        elif operation == "cleanup":
                            self._cleanup(connection, value)
                    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                connection.commit()
                if stop_after_batch:
                    break
        except sqlite3.Error as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            if connection is not None:
                connection.close()

    def _reindex(self, connection: sqlite3.Connection) -> None:
        for path in sorted(self.audit_directory.glob("guardian-*.jsonl")):
            try:
                with path.open("r", encoding="utf-8-sig") as stream:
                    for line in stream:
                        try:
                            document = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(document, dict):
                            self._insert_event(connection, document)
                connection.commit()
            except OSError:
                continue

    def _rebuild_operations(self, connection: sqlite3.Connection) -> None:
        """Rebuild operation/incident caches from the immutable event index."""

        connection.execute("DELETE FROM operations")
        connection.execute("DELETE FROM incidents")
        rows = connection.execute(
            "SELECT event_id,time,event_type,severity,component_id,"
            "subcomponent_id,summary,payload_json FROM events "
            "ORDER BY timestamp ASC"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row[7])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            document = {
                "event_id": row[0],
                "time": row[1],
                "event_type": row[2],
                "severity": row[3],
                "component_id": row[4],
                "subcomponent_id": row[5],
                "summary": row[6],
                "payload": payload,
            }
            self._upsert_derived_operation(connection, document, payload, row[1])
            self._upsert_derived_incident(connection, document, payload, row[1])
        self._finalize_legacy_recoveries(connection)
        connection.commit()

    @staticmethod
    def _finalize_legacy_recoveries(connection: sqlite3.Connection) -> None:
        operations = connection.execute(
            "SELECT operation_id,started_at,started_timestamp,status,initiator "
            "FROM operations WHERE operation_type='qmt_restart' "
            "AND incident_id='' AND context='legacy' "
            "ORDER BY started_timestamp ASC"
        ).fetchall()
        if not operations:
            return
        normalized: list[dict[str, Any]] = []
        for index, row in enumerate(operations):
            operation_id = str(row[0])
            started_at = str(row[1])
            started_ts = float(row[2])
            next_started_ts = (
                float(operations[index + 1][2])
                if index + 1 < len(operations)
                else None
            )
            result_row = connection.execute(
                "SELECT MAX(timestamp) FROM events WHERE event_id=? "
                "AND event_type IN ('recovery_result','manual_qmt_restart_result')",
                (operation_id,),
            ).fetchone()
            result_ts = float(result_row[0]) if result_row and result_row[0] else started_ts
            if next_started_ts is not None:
                stable = connection.execute(
                    "SELECT time,timestamp FROM events "
                    "WHERE event_type='state_transition' "
                    "AND timestamp > ? AND timestamp < ? "
                    "AND payload_json LIKE '%stable health verification completed%' "
                    "ORDER BY timestamp ASC LIMIT 1",
                    (result_ts, next_started_ts),
                ).fetchone()
            else:
                stable = connection.execute(
                    "SELECT time,timestamp FROM events "
                    "WHERE event_type='state_transition' "
                    "AND timestamp > ? "
                    "AND payload_json LIKE '%stable health verification completed%' "
                    "ORDER BY timestamp ASC LIMIT 1",
                    (result_ts,),
                ).fetchone()
            status = str(row[3])
            completed_at = ""
            completed_ts: float | None = None
            if stable is not None:
                status = "succeeded"
                completed_at = str(stable[0])
                completed_ts = float(stable[1])
            elif next_started_ts is not None and next_started_ts - started_ts <= 600:
                status = "failed"
                completed_ts = next_started_ts
                completed_at = datetime.fromtimestamp(
                    next_started_ts
                ).astimezone().isoformat()
            if completed_at:
                connection.execute(
                    "UPDATE operations SET status=?,phase='verification',"
                    "completed_at=?,completed_timestamp=?,duration_ms=? "
                    "WHERE operation_id=?",
                    (
                        status,
                        completed_at,
                        completed_ts,
                        max(0.0, (completed_ts - started_ts) * 1000),
                        operation_id,
                    ),
                )
            normalized.append(
                {
                    "operation_id": operation_id,
                    "started_at": started_at,
                    "started_ts": started_ts,
                    "status": status,
                    "initiator": str(row[4]),
                    "completed_at": completed_at,
                    "completed_ts": completed_ts,
                }
            )

        automatic_operations = [
            operation
            for operation in normalized
            if operation["initiator"] == "automatic"
        ]
        groups: list[list[dict[str, Any]]] = []
        for operation in automatic_operations:
            if not groups:
                groups.append([operation])
                continue
            previous = groups[-1][-1]
            same_incident = (
                previous["status"] == "failed"
                and operation["started_ts"] - previous["started_ts"] <= 600
            )
            if same_incident:
                groups[-1].append(operation)
            else:
                groups.append([operation])
        for number, group in enumerate(groups, start=1):
            first = group[0]
            last = group[-1]
            day = str(first["started_at"])[:10].replace("-", "")
            incident_id = f"QGI-LEGACY-{day}-{number:04d}"
            for attempt, operation in enumerate(group, start=1):
                connection.execute(
                    "UPDATE operations SET incident_id=?,attempt_no=?,context='legacy' "
                    "WHERE operation_id=?",
                    (incident_id, attempt, operation["operation_id"]),
                )
            resolved = last["status"] == "succeeded"
            resolved_at = str(last["completed_at"] or "") if resolved else ""
            resolved_ts = float(last["completed_ts"]) if resolved else None
            duration = (
                max(0.0, (resolved_ts - float(first["started_ts"])) * 1000)
                if resolved_ts is not None
                else None
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO incidents(
                    incident_id,detected_at,detected_timestamp,resolved_at,
                    resolved_timestamp,component_id,context,status,result,
                    attempt_count,duration_ms,summary,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    incident_id,
                    first["started_at"],
                    first["started_ts"],
                    resolved_at,
                    resolved_ts,
                    "qmt_api",
                    "legacy",
                    "resolved" if resolved else "open",
                    "succeeded" if resolved else "in_progress",
                    len(group),
                    duration,
                    "历史QMT恢复事件（由审计日志回填）",
                    json.dumps(
                        {
                            "legacy": True,
                            "operation_ids": [item["operation_id"] for item in group],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        document: dict[str, Any],
    ) -> None:
        event_id = str(document.get("event_id") or "")
        if not event_id:
            return
        time_value = str(document.get("time") or datetime.now().astimezone().isoformat())
        payload = document.get("payload")
        payload_value = payload if isinstance(payload, dict) else {"value": payload}
        component = str(
            document.get("component_id")
            or payload_value.get("component_id")
            or ""
        )
        subcomponent = str(
            document.get("subcomponent_id")
            or payload_value.get("subcomponent_id")
            or ""
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO events(
                event_id,time,timestamp,event_type,severity,component_id,
                subcomponent_id,summary,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                time_value,
                _timestamp(time_value),
                str(document.get("event_type") or "event"),
                str(document.get("severity") or "info"),
                component,
                subcomponent,
                _summary(document),
                json.dumps(payload_value, ensure_ascii=False, default=str),
            ),
        )
        MonitoringStore._upsert_derived_operation(
            connection,
            document,
            payload_value,
            time_value,
        )
        MonitoringStore._upsert_derived_incident(
            connection,
            document,
            payload_value,
            time_value,
        )

    @staticmethod
    def _operation_kind(event_type: str, payload: dict[str, Any]) -> str:
        explicit = str(payload.get("operation_type") or "")
        if explicit:
            return explicit
        if event_type in {
            "recovery_requested",
            "recovery_result",
            "recovery_verified",
            "recovery_verification_failed",
            "manual_qmt_restart_requested",
            "manual_qmt_restart_result",
            "manual_qmt_restart_verified",
            "manual_qmt_restart_verification_failed",
        }:
            return "qmt_restart"
        if event_type.startswith("manual_quantclass_restart"):
            return "quantclass_restart"
        if event_type.startswith("manual_check"):
            return "manual_check"
        if event_type == "monitor_thread_restarted":
            return "guardian_worker_restart"
        if event_type in {"recovery_paused", "recovery_resumed", "recovery_unlocked"}:
            return event_type.removeprefix("recovery_")
        if event_type == "settings_changed":
            return "settings_change"
        return ""

    @staticmethod
    def _operation_state(
        event_type: str,
        payload: dict[str, Any],
        operation_type: str,
    ) -> tuple[str, str, bool]:
        explicit_status = str(payload.get("status") or "")
        explicit_phase = str(payload.get("phase") or "")
        if explicit_status:
            terminal = explicit_status in {"succeeded", "failed", "blocked", "cancelled"}
            return explicit_status, explicit_phase, terminal
        if event_type.endswith("_requested"):
            return "in_progress", "requested", False
        if event_type in {"recovery_verified", "manual_qmt_restart_verified"}:
            return "succeeded", "verification", True
        if event_type in {
            "recovery_verification_failed",
            "manual_qmt_restart_verification_failed",
        }:
            return "failed", "verification", True
        if event_type.endswith("_rejected"):
            return "blocked", "safety", True
        if event_type.endswith("_result"):
            success = bool(payload.get("success"))
            if operation_type == "qmt_restart" and success:
                return "verifying", "launch", False
            return ("succeeded" if success else "failed"), "launch", True
        if event_type == "monitor_thread_restarted":
            return "succeeded", "completed", True
        if operation_type:
            return "succeeded", "completed", True
        return "", "", False

    @staticmethod
    def _upsert_derived_operation(
        connection: sqlite3.Connection,
        document: dict[str, Any],
        payload: dict[str, Any],
        time_value: str,
    ) -> None:
        event_type = str(document.get("event_type") or "")
        operation_type = MonitoringStore._operation_kind(event_type, payload)
        if not operation_type:
            return
        operation_id = str(
            payload.get("operation_id") or document.get("event_id") or ""
        )
        if not operation_id:
            return
        existing = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        existing_payload: dict[str, Any] = {}
        if existing is not None:
            try:
                existing_payload = json.loads(existing[15])
            except (TypeError, json.JSONDecodeError):
                existing_payload = {}
        merged_payload = {**existing_payload, **payload}
        status, phase, terminal = MonitoringStore._operation_state(
            event_type,
            payload,
            operation_type,
        )
        started_at = str(
            payload.get("started_at")
            or (existing[2] if existing is not None else "")
            or time_value
        )
        completed_at = str(
            payload.get("completed_at")
            or (time_value if terminal else "")
            or (existing[4] if existing is not None else "")
        )
        duration = payload.get("duration_ms")
        if not isinstance(duration, (int, float)) and completed_at:
            duration = max(
                0.0,
                (_timestamp(completed_at) - _timestamp(started_at)) * 1000,
            )
        default_initiator = (
            "watchdog"
            if event_type == "monitor_thread_restarted"
            else "manual"
            if event_type.startswith("manual_")
            else "automatic"
        )
        initiator = str(
            payload.get("initiator")
            or (existing[7] if existing is not None else "")
            or default_initiator
        )
        target = str(
            payload.get("target_component")
            or payload.get("component_id")
            or (existing[8] if existing is not None else "")
            or "quant_guardian"
        )
        summary = _summary(document) or (
            str(existing[14]) if existing is not None else ""
        )
        values = (
            operation_id,
            str(
                payload.get("incident_id")
                or (existing[1] if existing is not None else "")
            ),
            started_at,
            _timestamp(started_at),
            completed_at,
            _timestamp(completed_at) if completed_at else None,
            operation_type,
            initiator,
            target,
            str(
                payload.get("context")
                or (existing[9] if existing is not None else "")
                or (
                    "legacy"
                    if operation_type == "qmt_restart"
                    and not payload.get("operation_id")
                    else "production"
                )
            ),
            status or (str(existing[10]) if existing is not None else "in_progress"),
            phase or (str(existing[11]) if existing is not None else ""),
            int(
                payload.get("attempt_no")
                or (existing[12] if existing is not None else 1)
                or 1
            ),
            float(duration) if isinstance(duration, (int, float)) else None,
            summary[:500],
            json.dumps(merged_payload, ensure_ascii=False, default=str),
        )
        connection.execute(
            """
            INSERT INTO operations(
                operation_id,incident_id,started_at,started_timestamp,
                completed_at,completed_timestamp,operation_type,initiator,
                target_component,context,status,phase,attempt_no,duration_ms,
                summary,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(operation_id) DO UPDATE SET
                incident_id=excluded.incident_id,
                started_at=excluded.started_at,
                started_timestamp=excluded.started_timestamp,
                completed_at=excluded.completed_at,
                completed_timestamp=excluded.completed_timestamp,
                operation_type=excluded.operation_type,
                initiator=excluded.initiator,
                target_component=excluded.target_component,
                context=excluded.context,
                status=excluded.status,
                phase=excluded.phase,
                attempt_no=excluded.attempt_no,
                duration_ms=excluded.duration_ms,
                summary=excluded.summary,
                payload_json=excluded.payload_json
            """,
            values,
        )

    @staticmethod
    def _upsert_derived_incident(
        connection: sqlite3.Connection,
        document: dict[str, Any],
        payload: dict[str, Any],
        time_value: str,
    ) -> None:
        event_type = str(document.get("event_type") or "")
        incident_id = str(payload.get("incident_id") or "")
        if not incident_id:
            return
        existing = connection.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        existing_payload: dict[str, Any] = {}
        if existing is not None:
            try:
                existing_payload = json.loads(existing[12])
            except (TypeError, json.JSONDecodeError):
                existing_payload = {}
        merged = {**existing_payload, **payload}
        incident_event = event_type.startswith("incident_")
        resolved = event_type == "incident_resolved" or (
            incident_event
            and str(payload.get("status")) in {"resolved", "failed"}
        )
        detected_at = str(
            payload.get("started_at")
            or (existing[1] if existing is not None else "")
            or time_value
        )
        resolved_at = str(
            payload.get("resolved_at")
            or (time_value if resolved else "")
            or (existing[3] if existing is not None else "")
        )
        status = str(
            (payload.get("status") if incident_event else "")
            or (existing[7] if existing is not None else "")
            or ("resolved" if resolved else "open")
        )
        result = str(
            (payload.get("result") if incident_event else "")
            or (existing[8] if existing is not None else "")
            or ("succeeded" if resolved else "in_progress")
        )
        attempt_count = max(
            int(payload.get("attempt_count") or payload.get("attempt_no") or 0),
            int(existing[9]) if existing is not None else 0,
        )
        duration = payload.get("duration_ms")
        if not isinstance(duration, (int, float)) and resolved_at:
            duration = max(
                0.0,
                (_timestamp(resolved_at) - _timestamp(detected_at)) * 1000,
            )
        connection.execute(
            """
            INSERT INTO incidents(
                incident_id,detected_at,detected_timestamp,resolved_at,
                resolved_timestamp,component_id,context,status,result,
                attempt_count,duration_ms,summary,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(incident_id) DO UPDATE SET
                resolved_at=excluded.resolved_at,
                resolved_timestamp=excluded.resolved_timestamp,
                component_id=excluded.component_id,
                context=excluded.context,
                status=excluded.status,
                result=excluded.result,
                attempt_count=MAX(incidents.attempt_count, excluded.attempt_count),
                duration_ms=excluded.duration_ms,
                summary=excluded.summary,
                payload_json=excluded.payload_json
            """,
            (
                incident_id,
                detected_at,
                _timestamp(detected_at),
                resolved_at,
                _timestamp(resolved_at) if resolved_at else None,
                str(payload.get("component_id") or "qmt_api"),
                str(payload.get("context") or "production"),
                status,
                result,
                attempt_count,
                float(duration) if isinstance(duration, (int, float)) else None,
                _summary(document),
                json.dumps(merged, ensure_ascii=False, default=str),
            ),
        )

    @staticmethod
    def _insert_sample(
        connection: sqlite3.Connection,
        document: dict[str, Any],
    ) -> None:
        time_value = str(document.get("observed_at") or datetime.now().astimezone().isoformat())
        components = document.get("components")
        component_values = components if isinstance(components, dict) else {}
        qmt = component_values.get("qmt_api")
        trade = component_values.get("trade_system")
        qmt_value = qmt if isinstance(qmt, dict) else {}
        trade_value = trade if isinstance(trade, dict) else {}
        probe = document.get("probe")
        probe_value = probe if isinstance(probe, dict) else {}
        latency = probe_value.get("latency_ms")
        connection.execute(
            """
            INSERT INTO health_samples(
                time,timestamp,state,action,qmt_state,trade_state,latency_ms,payload_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                time_value,
                _timestamp(time_value),
                str(document.get("state") or "unknown"),
                str(document.get("action") or "none"),
                str(qmt_value.get("state") or "unknown"),
                str(trade_value.get("state") or "unknown"),
                float(latency) if isinstance(latency, (int, float)) else None,
                json.dumps(document, ensure_ascii=False, default=str),
            ),
        )

    def _cleanup(self, connection: sqlite3.Connection, now: datetime) -> None:
        cutoff = (now - timedelta(days=self.retention_days)).timestamp()
        connection.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        connection.execute("DELETE FROM health_samples WHERE timestamp < ?", (cutoff,))
        connection.execute(
            "DELETE FROM operations WHERE started_timestamp < ?", (cutoff,)
        )
        connection.execute(
            "DELETE FROM incidents WHERE detected_timestamp < ?", (cutoff,)
        )

    def _put(self, operation: str, value: Any) -> None:
        if not self.enabled or self._stop.is_set():
            return
        try:
            self._queue.put_nowait((operation, value))
        except queue.Full:
            self.last_error = "monitoring store queue is full"

    def enqueue_event(self, document: dict[str, Any]) -> None:
        self._put("event", dict(document))

    def enqueue_sample(self, document: dict[str, Any]) -> None:
        self._put("sample", dict(document))

    def request_cleanup(self, now: datetime | None = None) -> None:
        self._put("cleanup", now or datetime.now().astimezone())

    def _read_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    def fetch_events(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        search: str = "",
        severity: str = "all",
        component: str = "all",
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not self.enabled or not self.path.exists():
            return []
        since_value = since.timestamp() if since is not None else None
        until_value = until.timestamp() if until is not None else None
        severity_value = severity if severity and severity != "all" else None
        component_value = component if component and component != "all" else None
        search_value = search.strip() or None
        token = f"%{search_value or ''}%"
        query = (
            "SELECT * FROM events "
            "WHERE (? IS NULL OR timestamp >= ?) "
            "AND (? IS NULL OR timestamp < ?) "
            "AND (? IS NULL OR severity = ?) "
            "AND (? IS NULL OR component_id = ? OR component_id LIKE ?) "
            "AND (? IS NULL OR event_id LIKE ? OR event_type LIKE ? "
            "OR summary LIKE ? OR payload_json LIKE ?) "
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        params = [
            since_value,
            since_value,
            until_value,
            until_value,
            severity_value,
            severity_value,
            component_value,
            component_value,
            f"{component_value}.%" if component_value else "",
            search_value,
            token,
            token,
            token,
            token,
            max(1, min(int(limit), 1000)),
            max(0, int(offset)),
        ]
        connection: sqlite3.Connection | None = None
        try:
            connection = self._read_connection()
            rows = connection.execute(query, params).fetchall()
        except sqlite3.Error as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        finally:
            if connection is not None:
                connection.close()
        values: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {"raw": row["payload_json"]}
            values.append(
                {
                    "time": row["time"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "severity": row["severity"],
                    "component_id": row["component_id"],
                    "subcomponent_id": row["subcomponent_id"],
                    "summary": row["summary"],
                    "payload": payload,
                }
            )
        return values

    @staticmethod
    def _operation_document(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {"raw": row["payload_json"]}
        return {
            "operation_id": row["operation_id"],
            "incident_id": row["incident_id"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "operation_type": row["operation_type"],
            "initiator": row["initiator"],
            "target_component": row["target_component"],
            "context": row["context"],
            "status": row["status"],
            "phase": row["phase"],
            "attempt_no": row["attempt_no"],
            "duration_ms": row["duration_ms"],
            "summary": row["summary"],
            "payload": payload,
        }

    @staticmethod
    def _incident_document(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {"raw": row["payload_json"]}
        return {
            "incident_id": row["incident_id"],
            "detected_at": row["detected_at"],
            "resolved_at": row["resolved_at"],
            "component_id": row["component_id"],
            "context": row["context"],
            "status": row["status"],
            "result": row["result"],
            "attempt_count": row["attempt_count"],
            "duration_ms": row["duration_ms"],
            "summary": row["summary"],
            "payload": payload,
        }

    def fetch_operations(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        since: datetime | None = None,
        until: datetime | None = None,
        operation_type: str = "all",
        initiator: str = "all",
        status: str = "all",
        context: str = "all",
        search: str = "",
    ) -> list[dict[str, Any]]:
        if not self.enabled or not self.path.exists():
            return []
        since_value = since.timestamp() if since is not None else None
        until_value = until.timestamp() if until is not None else None
        operation_value = (
            operation_type if operation_type and operation_type != "all" else None
        )
        initiator_value = initiator if initiator and initiator != "all" else None
        status_value = status if status and status != "all" else None
        context_value = context if context and context != "all" else None
        search_value = search.strip() or None
        token = f"%{search_value or ''}%"
        query = (
            "SELECT * FROM operations "
            "WHERE (? IS NULL OR started_timestamp >= ?) "
            "AND (? IS NULL OR started_timestamp < ?) "
            "AND (? IS NULL OR operation_type = ?) "
            "AND (? IS NULL OR initiator = ?) "
            "AND (? IS NULL OR status = ?) "
            "AND (? IS NULL OR context = ?) "
            "AND (? IS NULL OR operation_id LIKE ? OR incident_id LIKE ? "
            "OR summary LIKE ? OR target_component LIKE ?) "
            "ORDER BY started_timestamp DESC LIMIT ? OFFSET ?"
        )
        params = [
            since_value,
            since_value,
            until_value,
            until_value,
            operation_value,
            operation_value,
            initiator_value,
            initiator_value,
            status_value,
            status_value,
            context_value,
            context_value,
            search_value,
            token,
            token,
            token,
            token,
            max(1, min(int(limit), 5000)),
            max(0, int(offset)),
        ]
        connection: sqlite3.Connection | None = None
        try:
            connection = self._read_connection()
            rows = connection.execute(query, params).fetchall()
            return [self._operation_document(row) for row in rows]
        except sqlite3.Error as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        finally:
            if connection is not None:
                connection.close()

    def fetch_operation_stats(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        context: str = "all",
    ) -> dict[str, Any]:
        empty = {
            "operations_total": 0,
            "qmt_restart_attempts": 0,
            "qmt_verified_attempts": 0,
            "attempt_success_rate": 0.0,
            "recovery_incidents": 0,
            "resolved_incidents": 0,
            "recovery_success_rate": 0.0,
            "repeated_incidents": 0,
            "blocked_operations": 0,
            "automatic_attempts": 0,
            "manual_attempts": 0,
            "median_mttr_ms": None,
            "p95_mttr_ms": None,
        }
        if not self.enabled or not self.path.exists():
            return empty
        until_value = until.timestamp() if until is not None else None
        context_value = context if context and context != "all" else None
        connection: sqlite3.Connection | None = None
        try:
            connection = self._read_connection()
            operations = connection.execute(
                "SELECT operation_type,initiator,status FROM operations "
                "WHERE started_timestamp >= ? "
                "AND (? IS NULL OR started_timestamp < ?) "
                "AND (? IS NULL OR context = ?)",
                (
                    since.timestamp(),
                    until_value,
                    until_value,
                    context_value,
                    context_value,
                ),
            ).fetchall()
            incidents = connection.execute(
                "SELECT status,result,attempt_count,duration_ms FROM incidents "
                "WHERE detected_timestamp >= ? AND component_id='qmt_api' "
                "AND (? IS NULL OR detected_timestamp < ?) "
                "AND (? IS NULL OR context = ?)",
                (
                    since.timestamp(),
                    until_value,
                    until_value,
                    context_value,
                    context_value,
                ),
            ).fetchall()
        except sqlite3.Error as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return empty
        finally:
            if connection is not None:
                connection.close()

        restarts = [
            row
            for row in operations
            if row["operation_type"] == "qmt_restart"
            and row["status"] not in {"blocked", "cancelled"}
        ]
        verified = [row for row in restarts if row["status"] == "succeeded"]
        recovery_incidents = [row for row in incidents if int(row["attempt_count"] or 0) > 0]
        resolved = [
            row
            for row in recovery_incidents
            if row["status"] == "resolved" and row["result"] == "succeeded"
        ]
        durations = sorted(
            float(row["duration_ms"])
            for row in resolved
            if isinstance(row["duration_ms"], (int, float))
        )

        def percentile(values: list[float], fraction: float) -> float | None:
            if not values:
                return None
            position = (len(values) - 1) * fraction
            lower = int(position)
            upper = min(len(values) - 1, lower + 1)
            weight = position - lower
            return values[lower] * (1 - weight) + values[upper] * weight

        result = dict(empty)
        result.update(
            {
                "operations_total": len(operations),
                "qmt_restart_attempts": len(restarts),
                "qmt_verified_attempts": len(verified),
                "attempt_success_rate": (
                    len(verified) / len(restarts) if restarts else 0.0
                ),
                "recovery_incidents": len(recovery_incidents),
                "resolved_incidents": len(resolved),
                "recovery_success_rate": (
                    len(resolved) / len(recovery_incidents)
                    if recovery_incidents
                    else 0.0
                ),
                "repeated_incidents": sum(
                    1
                    for row in recovery_incidents
                    if int(row["attempt_count"] or 0) > 1
                ),
                "blocked_operations": sum(
                    1 for row in operations if row["status"] == "blocked"
                ),
                "automatic_attempts": sum(
                    1 for row in restarts if row["initiator"] == "automatic"
                ),
                "manual_attempts": sum(
                    1 for row in restarts if row["initiator"] == "manual"
                ),
                "median_mttr_ms": percentile(durations, 0.5),
                "p95_mttr_ms": percentile(durations, 0.95),
            }
        )
        return result

    def fetch_operation_detail(self, operation_id: str) -> dict[str, Any]:
        if not self.enabled or not self.path.exists() or not operation_id:
            return {}
        connection: sqlite3.Connection | None = None
        try:
            connection = self._read_connection()
            operation_row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation_row is None:
                return {}
            operation = self._operation_document(operation_row)
            incident = None
            incident_id = str(operation.get("incident_id") or "")
            if incident_id:
                incident_row = connection.execute(
                    "SELECT * FROM incidents WHERE incident_id = ?",
                    (incident_id,),
                ).fetchone()
                if incident_row is not None:
                    incident = self._incident_document(incident_row)
            token = f"%{operation_id}%"
            event_rows = connection.execute(
                "SELECT * FROM events WHERE event_id = ? OR payload_json LIKE ? "
                "ORDER BY timestamp ASC",
                (operation_id, token),
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in event_rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = {"raw": row["payload_json"]}
                events.append(
                    {
                        "time": row["time"],
                        "event_id": row["event_id"],
                        "event_type": row["event_type"],
                        "severity": row["severity"],
                        "component_id": row["component_id"],
                        "summary": row["summary"],
                        "payload": payload,
                    }
                )
            if operation.get("context") == "legacy":
                completed_timestamp = operation_row["completed_timestamp"]
                if (
                    operation.get("status") == "succeeded"
                    and isinstance(completed_timestamp, (int, float))
                ):
                    stable_row = connection.execute(
                        "SELECT * FROM events WHERE event_type='state_transition' "
                        "AND timestamp BETWEEN ? AND ? "
                        "AND payload_json LIKE '%stable health verification completed%' "
                        "ORDER BY ABS(timestamp - ?) ASC LIMIT 1",
                        (
                            completed_timestamp - 1,
                            completed_timestamp + 1,
                            completed_timestamp,
                        ),
                    ).fetchone()
                    if stable_row is not None:
                        try:
                            stable_payload = json.loads(
                                stable_row["payload_json"]
                            )
                        except (TypeError, json.JSONDecodeError):
                            stable_payload = {
                                "raw": stable_row["payload_json"]
                            }
                        events.append(
                            {
                                "time": stable_row["time"],
                                "event_id": stable_row["event_id"],
                                "event_type": "legacy_stable_verification",
                                "severity": stable_row["severity"],
                                "component_id": stable_row["component_id"],
                                "summary": (
                                    "历史日志中的稳定健康验证通过"
                                ),
                                "payload": stable_payload,
                                "derived": True,
                            }
                        )
                elif operation.get("status") == "failed":
                    events.append(
                        {
                            "time": operation.get("completed_at") or "",
                            "event_id": "",
                            "event_type": "legacy_verification_inferred_failed",
                            "severity": "warning",
                            "component_id": "qmt_api",
                            "summary": (
                                "下一次恢复尝试开始前未找到稳定验证通过记录"
                            ),
                            "payload": {
                                "derived_from_legacy_audit": True,
                                "next_attempt_started": True,
                            },
                            "derived": True,
                        }
                    )
            events.sort(key=lambda event: str(event.get("time") or ""))
            return {"operation": operation, "incident": incident, "events": events}
        except sqlite3.Error as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {}
        finally:
            if connection is not None:
                connection.close()

    def fetch_samples(
        self,
        *,
        since: datetime,
        limit: int = 1200,
    ) -> list[dict[str, Any]]:
        if not self.enabled or not self.path.exists():
            return []
        connection: sqlite3.Connection | None = None
        try:
            connection = self._read_connection()
            rows = connection.execute(
                "SELECT payload_json FROM health_samples "
                "WHERE timestamp >= ? ORDER BY timestamp ASC",
                (since.timestamp(),),
            ).fetchall()
        except sqlite3.Error as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        finally:
            if connection is not None:
                connection.close()
        if len(rows) > limit:
            step = len(rows) / limit
            rows = [rows[min(len(rows) - 1, int(index * step))] for index in range(limit)]
        values: list[dict[str, Any]] = []
        for row in rows:
            try:
                document = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            if isinstance(document, dict):
                values.append(document)
        return values

    def close(self) -> None:
        self._stop.set()
        if self.enabled:
            try:
                self._queue.put(None, timeout=3)
            except queue.Full:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
