from __future__ import annotations

import unittest

from quant_guardian.domain.models import ProbeStatus, TradingPhase
from quant_guardian.probe.supervisor import ProbeObservation
from quant_guardian.reconciliation import decide_reconciliation


class ReconciliationTests(unittest.TestCase):
    def test_active_rocket_requires_manual(self) -> None:
        observation = ProbeObservation(
            ProbeStatus.HEALTHY,
            "ok",
            details={
                "orders": 0,
                "cancelable_orders": 0,
                "trades": 0,
                "positions": 1,
            },
        )
        decision = decide_reconciliation(
            observation,
            rocket_active=True,
            trading_phase=TradingPhase.TRADING,
            require_manual_resume=True,
        )
        self.assertTrue(decision.requires_manual)

    def test_cancelable_order_requires_manual(self) -> None:
        observation = ProbeObservation(
            ProbeStatus.HEALTHY,
            "ok",
            details={"cancelable_orders": 1},
        )
        decision = decide_reconciliation(
            observation,
            rocket_active=False,
            trading_phase=TradingPhase.CLOSED,
            require_manual_resume=True,
        )
        self.assertTrue(decision.requires_manual)


if __name__ == "__main__":
    unittest.main()