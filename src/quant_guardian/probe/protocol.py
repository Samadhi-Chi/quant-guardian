from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Operation = Literal["health", "reconcile", "calendar", "shutdown"]


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    operation: Operation
    userdata_directory: str
    xtquant_parent: str
    session_id: int
    account_id_protected: str = ""
    market: str = "SH"
    start_date: str = ""
    end_date: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> ProbeRequest:
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("probe request must be an object")
        allowed = {
            "operation",
            "userdata_directory",
            "xtquant_parent",
            "session_id",
            "account_id_protected",
            "market",
            "start_date",
            "end_date",
            "request_id",
        }
        if set(raw) - allowed:
            raise ValueError("probe request contains unknown fields")
        if raw.get("operation") not in {"health", "reconcile", "calendar", "shutdown"}:
            raise ValueError("unsupported probe operation")
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    request_id: str
    ok: bool
    status: str
    reason: str
    latency_ms: int = 0
    account_ref: str = ""
    account_status: str = "unknown"
    fatal: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> ProbeResponse:
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("probe response must be an object")
        allowed = {
            "request_id",
            "ok",
            "status",
            "reason",
            "latency_ms",
            "account_ref",
            "account_status",
            "fatal",
            "details",
        }
        if set(raw) - allowed:
            raise ValueError("probe response contains unknown fields")
        return cls(**raw)
