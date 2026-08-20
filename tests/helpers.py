from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from quant_guardian.domain.models import (
    LogSignal,
    ProbeStatus,
    ProcessStatus,
    RecoveryResult,
)
from quant_guardian.monitors.log_monitor import LogObservation
from quant_guardian.monitors.process_monitor import ProcessObservation
from quant_guardian.monitors.rocket_monitor import RocketObservation
from quant_guardian.probe.supervisor import ProbeObservation


@dataclass
class FakeProcessMonitor:
    status: ProcessStatus = ProcessStatus.HEALTHY

    def observe(self) -> ProcessObservation:
        return ProcessObservation(self.status, (), f"fake process {self.status.value}")


@dataclass
class FakeLogMonitor:
    signal: LogSignal = LogSignal.POSITIVE
    manual: bool = False

    def observe(self, now: datetime | None = None) -> LogObservation:
        return LogObservation(
            self.signal,
            f"fake log {self.signal.value}",
            login_requires_manual=self.manual,
        )


@dataclass
class FakeNetworkMonitor:
    available: bool = True

    def is_available(self) -> bool:
        return self.available


@dataclass
class FakeRocketMonitor:
    active: bool = False
    error_burst: bool = False
    business_healthy: bool | None = None
    business_age_seconds: float | None = 0
    business_health_known: bool = True

    def observe(self, now: datetime | None = None) -> RocketObservation:
        business_healthy = (
            self.active and not self.error_burst
            if self.business_healthy is None
            else self.business_healthy
        )
        return RocketObservation(
            self.active,
            self.error_burst,
            "fake rocket observation",
            0,
            business_healthy,
            self.business_age_seconds,
            "fake",
            self.business_health_known,
        )


class FakeProbe:
    def __init__(
        self,
        status: ProbeStatus = ProbeStatus.HEALTHY,
        reconcile_status: ProbeStatus = ProbeStatus.HEALTHY,
    ) -> None:
        self.status = status
        self.reconcile_status = reconcile_status
        self.stopped = False

    def health(self) -> ProbeObservation:
        return ProbeObservation(
            self.status,
            f"fake probe {self.status.value}",
            1,
            "ok" if self.status is ProbeStatus.HEALTHY else "unknown",
            "******42",
        )

    def reconcile(self) -> ProbeObservation:
        return ProbeObservation(
            self.reconcile_status,
            "fake reconciliation",
            2,
            "ok",
            "******42",
            {
                "orders": 0,
                "cancelable_orders": 0,
                "trades": 0,
                "positions": 1,
            },
        )

    def stop(self) -> None:
        self.stopped = True


class FakeRecovery:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.calls = 0
        self.manual_calls = 0

    def recover(self, snapshot, *, event_id: str) -> RecoveryResult:
        self.calls += 1
        return RecoveryResult(
            self.success,
            self.success,
            True,
            "fake launch success" if self.success else "fake launch failure",
        )

    def restart_manually(self, snapshot, *, event_id: str) -> RecoveryResult:
        self.manual_calls += 1
        return RecoveryResult(
            self.success,
            self.success,
            True,
            "fake manual launch success"
            if self.success
            else "fake manual launch failure",
        )


class FakeQuantclassController:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.calls = 0

    def restart(self, *, event_id: str) -> RecoveryResult:
        self.calls += 1
        return RecoveryResult(
            self.success,
            self.success,
            True,
            "fake Quantclass restart success"
            if self.success
            else "fake Quantclass restart failure",
        )
