from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class InboundMessage:
    channel: str
    message_key: str
    sender_id: str
    chat_id: str
    text: str = ""
    chat_type: str = "private"
    callback_id: str = ""
    callback_data: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now().astimezone())


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    message_id: int
    channel: str
    chat_id: str
    text: str
    buttons: tuple[tuple[tuple[str, str], ...], ...] = ()
    idempotency_key: str = ""
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    argument: str = ""


@dataclass(frozen=True, slots=True)
class CommandReply:
    text: str
    buttons: tuple[tuple[tuple[str, str], ...], ...] = ()
    command_name: str = ""
    outcome: str = "succeeded"


@dataclass(frozen=True, slots=True)
class Challenge:
    challenge_id: str
    request_id: str
    channel: str
    sender_id: str
    chat_id: str
    action: str
    code: str
    created_at: datetime
    expires_at: datetime
    status: str
    params: dict[str, Any] = field(default_factory=dict)
