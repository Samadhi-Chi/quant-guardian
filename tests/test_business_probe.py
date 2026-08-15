from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from quant_guardian.config import AppConfig
from quant_guardian.domain.models import ProbeStatus
from quant_guardian.probe.business import BusinessProbeManager
from quant_guardian.probe.supervisor import ProbeObservation


class FakeSupervisor:
    def __init__(self, status: ProbeStatus) -> None:
        self.status = status

    def reconcile(self) -> ProbeObservation:
        return ProbeObservation(
            self.status,
            "business summary",
            5,
            details=(
                {
                    "orders": 1,
                    "cancelable_orders": 0,
                    "trades": 1,
                    "positions": 2,
                }
                if self.status is ProbeStatus.HEALTHY
                else {}
            ),
        )

    def calendar(self, *, market: str, start_date: str, end_date: str) -> ProbeObservation:
        return ProbeObservation(
            ProbeStatus.HEALTHY,
            "calendar",
            1,
            details={"trading_dates": ["2026-08-10", "2026-08-11"]},
        )

    def stop(self) -> None:
        return None


class BusinessProbeTests(unittest.TestCase):
    def test_calendar_coverage_uses_last_returned_date_not_requested_day(self) -> None:
        config = AppConfig()
        observed: list[tuple[list[str], date]] = []
        manager = BusinessProbeManager(
            config,
            calendar_listener=lambda values, coverage: observed.append(
                (values, coverage)
            ),
        )
        manager.supervisor = FakeSupervisor(ProbeStatus.HEALTHY)  # type: ignore[assignment]
        manager._running = True
        manager._run(
            datetime(2026, 8, 12, 0, 30, tzinfo=UTC),
            True,
            manager._generation,
        )
        self.assertEqual(observed[0][1], date(2026, 8, 11))

    def test_recovery_revalidation_retries_fast_until_success(self) -> None:
        config = AppConfig()
        config.monitoring.anomaly_retry_seconds = 15
        config.monitoring.business_summary_retry_seconds = 300
        manager = BusinessProbeManager(config)
        now = datetime(2026, 8, 10, 19, 0, tzinfo=UTC)
        original_supervisor = manager.supervisor
        manager.invalidate_after_recovery(now)
        self.assertIsNot(manager.supervisor, original_supervisor)
        self.assertNotEqual(
            manager.supervisor.probe_config.session_id,
            original_supervisor.probe_config.session_id,
        )

        manager.supervisor = FakeSupervisor(ProbeStatus.TIMEOUT)  # type: ignore[assignment]
        manager._running = True
        manager._run(now, False, manager._generation)
        self.assertEqual(manager.latest["status"], "unavailable")
        self.assertTrue(manager._recovery_revalidation)
        self.assertIsNotNone(manager._next_due)
        delay = (manager._next_due - datetime.now().astimezone()).total_seconds()
        self.assertGreater(delay, 13)
        self.assertLessEqual(delay, 15)

        manager.supervisor = FakeSupervisor(ProbeStatus.HEALTHY)  # type: ignore[assignment]
        manager._running = True
        manager._run(now, False, manager._generation)
        self.assertEqual(manager.latest["status"], "healthy")
        self.assertFalse(manager._recovery_revalidation)

    def test_failed_summary_preserves_last_good_counts_and_rotates_session(self) -> None:
        config = AppConfig()
        manager = BusinessProbeManager(config)
        now = datetime(2026, 8, 10, 19, 0, tzinfo=UTC)
        manager.supervisor.stop()
        manager.supervisor = FakeSupervisor(ProbeStatus.HEALTHY)  # type: ignore[assignment]
        manager._running = True
        manager._run(now, False, manager._generation)
        self.assertEqual(manager.latest["orders"], 1)

        manager.supervisor = FakeSupervisor(ProbeStatus.TIMEOUT)  # type: ignore[assignment]
        manager._running = True
        manager._run(now, False, manager._generation)
        self.assertEqual(manager.latest["status"], "stale")
        self.assertEqual(manager.latest["orders"], 1)
        self.assertEqual(manager.latest["consecutive_failures"], 1)

        old_generation = manager._generation
        manager._running = True
        manager._run(now, False, manager._generation)
        self.assertGreater(manager._generation, old_generation)
        self.assertEqual(manager.latest["status"], "stale")
        self.assertEqual(manager.latest["orders"], 1)
        self.assertEqual(manager.latest["consecutive_failures"], 2)
        manager.stop()


if __name__ == "__main__":
    unittest.main()
