from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class GuardianState(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    VERIFYING = "verifying"
    MANUAL_REQUIRED = "manual_required"
    LOCKOUT = "lockout"
    PAUSED = "paused"


class RecommendedAction(StrEnum):
    NONE = "none"
    WAIT = "wait"
    WAIT_NETWORK = "wait_network"
    RECOVER_QMT = "recover_qmt"
    VERIFY = "verify"
    REQUIRE_MANUAL = "require_manual"
    LOCKOUT = "lockout"


class ProcessStatus(StrEnum):
    HEALTHY = "healthy"
    MISSING = "missing"
    UNRESPONSIVE = "unresponsive"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNKNOWN = "unknown"


class ProbeStatus(StrEnum):
    HEALTHY = "healthy"
    FAILED = "failed"
    TIMEOUT = "timeout"
    STARTING = "starting"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class LogSignal(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    EXPLICIT_DISCONNECT = "explicit_disconnect"
    LOGIN_FAILURE = "login_failure"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class TradingPhase(StrEnum):
    CLOSED = "closed"
    PREMARKET = "premarket"
    TRADING = "trading"
    BREAK = "break"
    POSTMARKET = "postmarket"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    observed_at: datetime
    process_status: ProcessStatus
    probe_status: ProbeStatus
    log_signal: LogSignal = LogSignal.NEUTRAL
    network_available: bool = True
    rocket_active: bool = False
    trading_phase: TradingPhase = TradingPhase.CLOSED
    account_status: str = "unknown"
    login_requires_manual: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return (
            self.process_status is ProcessStatus.HEALTHY
            and self.probe_status is ProbeStatus.HEALTHY
            and self.log_signal
            not in {LogSignal.EXPLICIT_DISCONNECT, LogSignal.LOGIN_FAILURE}
            and self.network_available
            and not self.login_requires_manual
        )

    @property
    def is_immediate_failure(self) -> bool:
        return self.process_status in {
            ProcessStatus.MISSING,
            ProcessStatus.IDENTITY_MISMATCH,
        }

    @property
    def has_explicit_disconnect(self) -> bool:
        return self.log_signal in {
            LogSignal.EXPLICIT_DISCONNECT,
            LogSignal.LOGIN_FAILURE,
        }


@dataclass(frozen=True, slots=True)
class Transition:
    at: datetime
    old_state: GuardianState
    new_state: GuardianState
    action: RecommendedAction
    reason: str
    snapshot: HealthSnapshot | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    success: bool
    launched: bool
    live_action: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
