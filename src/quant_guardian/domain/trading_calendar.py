from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from quant_guardian.config import MonitoringConfig, TradingConfig
from quant_guardian.domain.models import TradingPhase


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


# Shanghai Stock Exchange annual closure notice.  Weekends are deliberately
# included so the set can also be rendered verbatim in diagnostics.
BUILTIN_CLOSED_DATES: dict[int, frozenset[str]] = {
    2026: frozenset(
        {
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-04",
            "2026-02-14",
            "2026-02-15",
            "2026-02-16",
            "2026-02-17",
            "2026-02-18",
            "2026-02-19",
            "2026-02-20",
            "2026-02-21",
            "2026-02-22",
            "2026-02-23",
            "2026-02-28",
            "2026-04-04",
            "2026-04-05",
            "2026-04-06",
            "2026-05-01",
            "2026-05-02",
            "2026-05-03",
            "2026-05-04",
            "2026-05-05",
            "2026-05-09",
            "2026-06-19",
            "2026-06-20",
            "2026-06-21",
            "2026-09-20",
            "2026-09-25",
            "2026-09-26",
            "2026-09-27",
            "2026-10-01",
            "2026-10-02",
            "2026-10-03",
            "2026-10-04",
            "2026-10-05",
            "2026-10-06",
            "2026-10-07",
            "2026-10-10",
        }
    )
}


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    mode: str
    interval_seconds: float
    trading_day: bool
    source: str
    uncertain: bool = False


class TradingCalendar:
    def __init__(
        self,
        config: TradingConfig,
        monitoring: MonitoringConfig | None = None,
    ) -> None:
        self.config = config
        self.monitoring = monitoring or MonitoringConfig()
        self.timezone = ZoneInfo(config.timezone)
        self.manual_closed_dates = set(config.holidays) | set(
            config.manual_closed_dates
        )
        self.manual_open_dates = set(config.manual_open_dates)
        self.premarket_start = _parse_time(config.premarket_start)
        self.morning_start = _parse_time(config.morning_start)
        self.morning_end = _parse_time(config.morning_end)
        self.afternoon_start = _parse_time(config.afternoon_start)
        self.afternoon_end = _parse_time(config.afternoon_end)
        self.postmarket_end = _parse_time(config.postmarket_end)
        self.active_start = _parse_time(self.monitoring.active_start)
        self.active_end = _parse_time(self.monitoring.active_end)
        self._market_dates: set[str] = set()
        self._market_coverage_end: date | None = None

    def update_market_dates(
        self,
        values: list[str] | set[str] | tuple[str, ...],
        *,
        coverage_end: date | None = None,
    ) -> None:
        valid = {value for value in values if len(value) == 10}
        if valid:
            self._market_dates.update(valid)
        if coverage_end:
            # A fresh QMT calendar response is authoritative for how far the
            # returned list actually extends.  It may legitimately move the
            # boundary backwards when a midnight query accepted today's end
            # date but only returned completed dates through yesterday.
            self._market_coverage_end = coverage_end

    def _trading_day_info(self, day: date) -> tuple[bool, str, bool]:
        key = day.isoformat()
        if key in self.manual_open_dates:
            return True, "manual-open", False
        if key in self.manual_closed_dates:
            return False, "manual-closed", False
        if day.weekday() >= 5:
            return False, "weekend", False
        builtin = BUILTIN_CLOSED_DATES.get(day.year)
        if builtin is not None and key in builtin:
            return False, "official-calendar", False
        if self._market_coverage_end and day <= self._market_coverage_end:
            return key in self._market_dates, "qmt-calendar-cache", False
        if builtin is None:
            return True, "weekday-fallback", True
        return True, "official-calendar", False

    def is_trading_day(self, moment: datetime) -> bool:
        localized = moment.astimezone(self.timezone)
        return self._trading_day_info(localized.date())[0]

    def schedule_at(self, moment: datetime) -> ScheduleDecision:
        localized = moment.astimezone(self.timezone)
        trading_day, source, uncertain = self._trading_day_info(localized.date())
        current = localized.time().replace(tzinfo=None)
        active = trading_day and self.active_start <= current < self.active_end
        return ScheduleDecision(
            mode="active" if active else "idle",
            interval_seconds=(
                self.monitoring.active_interval_seconds
                if active
                else self.monitoring.idle_interval_seconds
            ),
            trading_day=trading_day,
            source=source,
            uncertain=uncertain,
        )

    def next_check_at(self, moment: datetime, *, anomalous: bool = False) -> datetime:
        decision = self.schedule_at(moment)
        seconds = (
            self.monitoring.anomaly_retry_seconds
            if anomalous and decision.mode == "idle"
            else decision.interval_seconds
        )
        scheduled = moment + timedelta(seconds=seconds)
        if decision.mode == "active":
            return scheduled

        # An hourly idle check must never jump over the beginning of the next
        # active trading window.  This matters most around 08:30: a check at
        # 08:18 should wake at 08:30, not sleep until 09:18.
        localized = moment.astimezone(self.timezone)
        for offset in range(0, 370):
            day = localized.date() + timedelta(days=offset)
            if not self._trading_day_info(day)[0]:
                continue
            boundary = datetime.combine(day, self.active_start, self.timezone)
            if boundary <= localized:
                continue
            boundary_in_source_zone = boundary.astimezone(moment.tzinfo)
            return min(scheduled, boundary_in_source_zone)
        return scheduled

    def phase_at(self, moment: datetime) -> TradingPhase:
        localized = moment.astimezone(self.timezone)
        if not self._trading_day_info(localized.date())[0]:
            return TradingPhase.CLOSED
        current = localized.time().replace(tzinfo=None)
        if current < self.premarket_start:
            return TradingPhase.CLOSED
        if current < self.morning_start:
            return TradingPhase.PREMARKET
        if current <= self.morning_end:
            return TradingPhase.TRADING
        if current < self.afternoon_start:
            return TradingPhase.BREAK
        if current <= self.afternoon_end:
            return TradingPhase.TRADING
        if current <= self.postmarket_end:
            return TradingPhase.POSTMARKET
        return TradingPhase.CLOSED
