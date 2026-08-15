from __future__ import annotations

from dataclasses import dataclass

from quant_guardian.domain.models import ProbeStatus, TradingPhase
from quant_guardian.probe.supervisor import ProbeObservation


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    requires_manual: bool
    reason: str
    details: dict[str, object]


def decide_reconciliation(
    observation: ProbeObservation,
    *,
    rocket_active: bool,
    trading_phase: TradingPhase,
    require_manual_resume: bool,
) -> ReconciliationDecision:
    details = dict(observation.details or {})
    if observation.status is not ProbeStatus.HEALTHY:
        return ReconciliationDecision(
            True,
            "read-only reconciliation did not complete successfully",
            details,
        )
    if any(value == "unknown" for value in details.values()):
        return ReconciliationDecision(
            True,
            "one or more broker reconciliation results are ambiguous",
            details,
        )
    cancelable = details.get("cancelable_orders", 0)
    if isinstance(cancelable, int) and cancelable > 0:
        return ReconciliationDecision(
            True,
            "broker reports unfinished or cancelable orders",
            details,
        )
    if rocket_active and require_manual_resume:
        phase_text = (
            "during trading"
            if trading_phase is TradingPhase.TRADING
            else "while Rocket was active"
        )
        return ReconciliationDecision(
            True,
            f"QMT recovered {phase_text}; Rocket requires explicit user acknowledgement",
            details,
        )
    return ReconciliationDecision(
        False, "no manual reconciliation gate is required", details
    )