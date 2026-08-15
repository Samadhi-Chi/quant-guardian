from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    severity: str
    event_key: str
    at: datetime


class NotificationCenter:
    def __init__(self, dedupe_minutes: int = 10) -> None:
        self.dedupe_window = timedelta(minutes=dedupe_minutes)
        self._last_sent: dict[tuple[str, str], datetime] = {}
        self._listeners: list[Callable[[Notification], None]] = []
        self._lock = Lock()

    def subscribe(self, listener: Callable[[Notification], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def publish(
        self,
        title: str,
        message: str,
        *,
        severity: str = "info",
        event_key: str,
        now: datetime | None = None,
    ) -> bool:
        at = now or datetime.now().astimezone()
        key = (event_key, severity)
        with self._lock:
            last = self._last_sent.get(key)
            if last and at - last < self.dedupe_window:
                return False
            self._last_sent[key] = at
            listeners = tuple(self._listeners)
        notification = Notification(title, message, severity, event_key, at)
        for listener in listeners:
            listener(notification)
        return True