from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from quant_guardian.diagnostics.redaction import redact
from quant_guardian.domain.models import Transition


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported audit value: {type(value)!r}")


class AuditLogger:
    def __init__(self, log_directory: Path, retention_days: int = 30) -> None:
        self.log_directory = log_directory
        self.retention_days = retention_days
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def _path_for(self, moment: datetime) -> Path:
        return self.log_directory / f"guardian-{moment:%Y%m%d}.jsonl"

    def record(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        severity: str = "info",
        moment: datetime | None = None,
        event_id: str | None = None,
    ) -> str:
        at = moment or datetime.now().astimezone()
        identifier = event_id or f"QG-{at:%Y%m%d}-{uuid.uuid4().hex[:8]}"
        document = redact(
            {
                "schema_version": 1,
                "event_id": identifier,
                "time": at,
                "event_type": event_type,
                "severity": severity,
                "payload": payload,
            }
        )
        line = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        )
        with self._lock:
            with self._path_for(at).open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
        for listener in tuple(self._listeners):
            try:
                listener(document)
            except Exception:
                # Audit persistence is canonical. A rebuildable index/listener
                # must never break the monitoring or recovery path.
                continue
        return identifier

    def record_transition(
        self,
        transition: Transition,
        *,
        incident_id: str = "",
        operation_id: str = "",
    ) -> str:
        return self.record(
            "state_transition",
            {
                "component_id": "qmt_api",
                "incident_id": incident_id,
                "operation_id": operation_id,
                "old_state": transition.old_state,
                "new_state": transition.new_state,
                "action": transition.action,
                "reason": transition.reason,
                "snapshot": transition.snapshot,
            },
            severity=(
                "critical"
                if transition.new_state.value in {"manual_required", "lockout"}
                else "warning"
                if transition.new_state.value in {"suspect", "degraded", "recovering"}
                else "info"
            ),
            moment=transition.at,
        )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        files = sorted(self.log_directory.glob("guardian-*.jsonl"), reverse=True)
        for path in files:
            lines = self._tail_lines(path, max(1, limit - len(records)))
            for line in lines:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(records) >= limit:
                    return records
        return records

    @staticmethod
    def _tail_lines(path: Path, limit: int) -> list[str]:
        """Read only the final JSONL records instead of the complete day file."""
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                position = stream.tell()
                buffer = b""
                while position > 0 and buffer.count(b"\n") <= limit:
                    size = min(65_536, position)
                    position -= size
                    stream.seek(position)
                    buffer = stream.read(size) + buffer
        except OSError:
            return []
        raw_lines = buffer.splitlines()
        values: list[str] = []
        for raw in reversed(raw_lines):
            if not raw.strip():
                continue
            values.append(raw.decode("utf-8-sig", errors="replace"))
            if len(values) >= limit:
                break
        return values

    def cleanup(self, now: datetime | None = None) -> int:
        at = now or datetime.now().astimezone()
        cutoff = at - timedelta(days=self.retention_days)
        removed = 0
        for path in self.log_directory.glob("guardian-*.jsonl"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=at.tzinfo)
                if modified < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
