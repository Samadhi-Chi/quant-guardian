from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

from quant_guardian.gateway.models import InboundMessage, OutboundMessage


class ChannelError(RuntimeError):
    pass


class AuthenticationError(ChannelError):
    pass


class ChannelAdapter(ABC):
    name: str

    @abstractmethod
    def run(
        self,
        stop_event: threading.Event,
        on_message: Callable[[InboundMessage], None],
    ) -> None: ...

    @abstractmethod
    def send(self, message: OutboundMessage) -> str: ...
