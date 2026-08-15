from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from quant_guardian.config import MonitoringConfig, TradingConfig
from quant_guardian.domain.models import TradingPhase
from quant_guardian.domain.trading_calendar import TradingCalendar

TZ = ZoneInfo("Asia/Shanghai")


class TradingCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = TradingCalendar(TradingConfig())

    def test_weekend_is_closed(self) -> None:
        moment = datetime(2026, 8, 9, 10, 0, tzinfo=TZ)
        self.assertEqual(self.calendar.phase_at(moment), TradingPhase.CLOSED)

    def test_trading_and_break_phases(self) -> None:
        trading = datetime(2026, 8, 10, 10, 0, tzinfo=TZ)
        lunch = datetime(2026, 8, 10, 12, 0, tzinfo=TZ)
        self.assertEqual(self.calendar.phase_at(trading), TradingPhase.TRADING)
        self.assertEqual(self.calendar.phase_at(lunch), TradingPhase.BREAK)

    def test_scheduler_boundaries_and_lunch_are_high_frequency(self) -> None:
        monitoring = MonitoringConfig(
            active_interval_seconds=5,
            idle_interval_seconds=3600,
        )
        calendar = TradingCalendar(TradingConfig(), monitoring)
        cases = (
            (datetime(2026, 8, 10, 8, 29, 59, tzinfo=TZ), "idle", 3600),
            (datetime(2026, 8, 10, 8, 30, tzinfo=TZ), "active", 5),
            (datetime(2026, 8, 10, 12, 0, tzinfo=TZ), "active", 5),
            (datetime(2026, 8, 10, 16, 29, 59, tzinfo=TZ), "active", 5),
            (datetime(2026, 8, 10, 16, 30, tzinfo=TZ), "idle", 3600),
        )
        for moment, mode, interval in cases:
            with self.subTest(moment=moment):
                decision = calendar.schedule_at(moment)
                self.assertEqual(decision.mode, mode)
                self.assertEqual(decision.interval_seconds, interval)

    def test_idle_check_is_clamped_to_next_active_boundary(self) -> None:
        monitoring = MonitoringConfig(
            active_interval_seconds=5,
            idle_interval_seconds=3600,
        )
        calendar = TradingCalendar(TradingConfig(), monitoring)
        before_open = datetime(2026, 8, 10, 8, 18, tzinfo=TZ)
        self.assertEqual(
            calendar.next_check_at(before_open),
            datetime(2026, 8, 10, 8, 30, tzinfo=TZ),
        )
        after_close = datetime(2026, 8, 10, 16, 31, tzinfo=TZ)
        self.assertEqual(
            calendar.next_check_at(after_close),
            after_close + timedelta(hours=1),
        )

    def test_idle_check_before_open_on_holiday_does_not_use_closed_boundary(self) -> None:
        monitoring = MonitoringConfig(idle_interval_seconds=3600)
        calendar = TradingCalendar(TradingConfig(), monitoring)
        holiday = datetime(2026, 2, 18, 8, 18, tzinfo=TZ)
        self.assertEqual(
            calendar.next_check_at(holiday),
            holiday + timedelta(hours=1),
        )

    def test_official_holiday_and_manual_overrides(self) -> None:
        holiday = datetime(2026, 2, 18, 10, 0, tzinfo=TZ)
        self.assertFalse(self.calendar.schedule_at(holiday).trading_day)
        opened = TradingConfig(manual_open_dates=["2026-02-18"])
        self.assertEqual(
            TradingCalendar(opened).schedule_at(holiday).mode,
            "active",
        )
        closed = TradingConfig(manual_closed_dates=["2026-08-10"])
        self.assertEqual(
            TradingCalendar(closed).schedule_at(
                datetime(2026, 8, 10, 10, 0, tzinfo=TZ)
            ).mode,
            "idle",
        )

    def test_unknown_calendar_year_uses_conservative_weekday_fallback(self) -> None:
        decision = self.calendar.schedule_at(
            datetime(2027, 1, 4, 10, 0, tzinfo=TZ)
        )
        self.assertEqual(decision.mode, "active")
        self.assertTrue(decision.uncertain)
        self.assertEqual(decision.source, "weekday-fallback")

    def test_incomplete_qmt_cache_does_not_close_next_weekday(self) -> None:
        self.calendar.update_market_dates(
            ["2026-08-10", "2026-08-11"],
            coverage_end=date(2026, 8, 11),
        )
        decision = self.calendar.schedule_at(
            datetime(2026, 8, 12, 10, 0, tzinfo=TZ)
        )
        self.assertTrue(decision.trading_day)
        self.assertEqual(decision.mode, "active")
        self.assertEqual(decision.source, "official-calendar")

    def test_fresh_qmt_boundary_can_correct_stale_future_coverage(self) -> None:
        self.calendar.update_market_dates(
            ["2026-08-10", "2026-08-11"],
            coverage_end=date(2026, 8, 12),
        )
        self.assertFalse(
            self.calendar.schedule_at(
                datetime(2026, 8, 12, 10, 0, tzinfo=TZ)
            ).trading_day
        )
        self.calendar.update_market_dates(
            ["2026-08-10", "2026-08-11"],
            coverage_end=date(2026, 8, 11),
        )
        corrected = self.calendar.schedule_at(
            datetime(2026, 8, 12, 10, 0, tzinfo=TZ)
        )
        self.assertTrue(corrected.trading_day)
        self.assertEqual(corrected.mode, "active")


if __name__ == "__main__":
    unittest.main()
