from __future__ import annotations

from datetime import datetime, timedelta

from quant_guardian.domain.models import (
    HealthSnapshot,
    LogSignal,
    ProbeStatus,
    ProcessStatus,
)
from quant_guardian.domain.state_machine import (
    GuardianStateMachine,
    StateMachinePolicy,
)


def run_simulation() -> list[dict[str, str]]:
    now = datetime.now().astimezone()
    policy = StateMachinePolicy(
        startup_grace_seconds=0,
        verify_successes=2,
        verify_min_span_seconds=1,
        backoff_seconds=(1, 2, 5, 10),
    )
    machine = GuardianStateMachine(policy, now=now)
    sequence = [
        (0, ProcessStatus.HEALTHY, ProbeStatus.HEALTHY, LogSignal.POSITIVE),
        (1, ProcessStatus.HEALTHY, ProbeStatus.HEALTHY, LogSignal.POSITIVE),
        (5, ProcessStatus.HEALTHY, ProbeStatus.TIMEOUT, LogSignal.NEUTRAL),
        (10, ProcessStatus.HEALTHY, ProbeStatus.FAILED, LogSignal.EXPLICIT_DISCONNECT),
        (20, ProcessStatus.HEALTHY, ProbeStatus.FAILED, LogSignal.EXPLICIT_DISCONNECT),
    ]
    output: list[dict[str, str]] = []
    for offset, process, probe, log in sequence:
        transition = machine.observe(
            HealthSnapshot(
                observed_at=now + timedelta(seconds=offset),
                process_status=process,
                probe_status=probe,
                log_signal=log,
            ),
            recovery_permitted=False,
        )
        output.append(
            {
                "time": transition.at.isoformat(),
                "old_state": transition.old_state.value,
                "new_state": transition.new_state.value,
                "action": transition.action.value,
                "reason": transition.reason,
            }
        )
    return output