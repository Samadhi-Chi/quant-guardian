from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta

from quant_guardian.config import AppConfig
from quant_guardian.domain.models import ProbeStatus
from quant_guardian.probe.supervisor import ProbeSupervisor


class BusinessProbeManager:
    """Low-frequency isolated read-only business summary worker."""

    def __init__(
        self,
        config: AppConfig,
        *,
        calendar_listener: Callable[[list[str], date], None] | None = None,
    ) -> None:
        probe_config = replace(
            config.probe,
            session_id=config.probe.session_id + 137,
            timeout_seconds=config.monitoring.business_summary_timeout_seconds,
        )
        self._probe_config = probe_config
        self._qmt_config = config.qmt
        self._generation = 0
        self._used_session_ids = {config.probe.session_id}
        self.supervisor = self._new_supervisor()
        self.monitoring = config.monitoring
        self.market = config.trading.market
        self.calendar_listener = calendar_listener
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._next_due: datetime | None = None
        self._calendar_refreshed: date | None = None
        self._recovery_revalidation = False
        self._consecutive_failures = 0
        self._failures_since_rotation = 0
        self._last_success: dict[str, object] | None = None
        self._latest: dict[str, object] = {
            "status": "pending",
            "reason": "等待首次只读委托汇总",
            "latency_ms": 0,
        }

    def _new_supervisor(self) -> ProbeSupervisor:
        # XtQuant identifies clients by session ID. Reusing the same ID after
        # the QMT process changes can make a newly spawned worker hang even
        # though a genuinely new ID returns immediately.
        if self._generation == 0:
            session_id = self._probe_config.session_id
        else:
            session_id = self._probe_config.session_id
            while session_id in self._used_session_ids:
                session_id = 100_000_000 + secrets.randbelow(1_900_000_000)
        self._used_session_ids.add(session_id)
        probe_config = replace(
            self._probe_config,
            session_id=session_id,
        )
        return ProbeSupervisor(probe_config, self._qmt_config)

    @property
    def latest(self) -> dict[str, object]:
        with self._lock:
            return dict(self._latest)

    def maybe_request(self, now: datetime, *, active: bool) -> None:
        with self._lock:
            if self._running or (self._next_due and now < self._next_due):
                return
            self._running = True
            interval = (
                self.monitoring.business_summary_interval_seconds
                if active
                else self.monitoring.idle_interval_seconds
            )
            self._next_due = now + timedelta(seconds=interval)
            refresh_calendar = self._calendar_refreshed != now.date()
            generation = self._generation
        self._thread = threading.Thread(
            target=self._run,
            args=(now, refresh_calendar, generation),
            name="quant-guardian-business-probe",
            daemon=True,
        )
        self._thread.start()

    def _run(
        self,
        now: datetime,
        refresh_calendar: bool,
        generation: int,
    ) -> None:
        with self._lock:
            supervisor = self.supervisor
        observation = supervisor.reconcile()
        details = dict(observation.details or {})
        result: dict[str, object] = {
            "status": (
                "healthy"
                if observation.status is ProbeStatus.HEALTHY
                else "unavailable"
            ),
            "reason": observation.reason,
            "latency_ms": observation.latency_ms,
            "sampled_at": datetime.now().astimezone().isoformat(),
            **details,
        }
        retry = observation.status is not ProbeStatus.HEALTHY
        old_supervisor: ProbeSupervisor | None = None
        if refresh_calendar and not retry:
            calendar = supervisor.calendar(
                market=self.market,
                start_date=f"{now:%Y}0101",
                end_date=f"{now:%Y%m%d}",
            )
            calendar_details = dict(calendar.details or {})
            values = calendar_details.get("trading_dates")
            if (
                calendar.status is ProbeStatus.HEALTHY
                and isinstance(values, list)
                and values
            ):
                dates = [str(value) for value in values]
                # Around midnight some QMT builds accept today's end date but
                # only return completed trading dates through yesterday.  Do
                # not mark the missing weekday as a confirmed market closure;
                # the calendar can then conservatively use the built-in
                # exchange schedule for the uncovered day.
                try:
                    coverage = max(date.fromisoformat(value) for value in dates)
                except ValueError:
                    coverage = now.date() - timedelta(days=1)
                if self.calendar_listener:
                    try:
                        self.calendar_listener(dates, coverage)
                    except Exception:
                        pass
                with self._lock:
                    self._calendar_refreshed = now.date()
        with self._lock:
            if generation != self._generation:
                # A QMT recovery happened while this request was in flight.
                # Never publish a result produced by the pre-recovery session.
                self._running = False
                self._next_due = datetime.now().astimezone()
                return
            if retry:
                self._consecutive_failures += 1
                self._failures_since_rotation += 1
                failed_at = str(result.get("sampled_at") or "")
                if self._last_success:
                    result = {
                        **self._last_success,
                        "status": "stale",
                        "reason": (
                            f"最新只读业务汇总失败：{observation.reason}；"
                            "保留上一次有效结果"
                        ),
                        "last_success_at": self._last_success.get("sampled_at", ""),
                        "last_attempt_at": failed_at,
                        "last_error": observation.reason,
                        "consecutive_failures": self._consecutive_failures,
                        "stale": True,
                    }
                else:
                    result = {
                        **result,
                        "consecutive_failures": self._consecutive_failures,
                        "last_attempt_at": failed_at,
                        "stale": True,
                    }
                retry_seconds = (
                    self.monitoring.anomaly_retry_seconds
                    if self._recovery_revalidation
                    else self.monitoring.business_summary_retry_seconds
                )
                self._next_due = datetime.now().astimezone() + timedelta(
                    seconds=retry_seconds
                )
                result["next_retry_at"] = self._next_due.isoformat()
                if self._failures_since_rotation >= 2:
                    old_supervisor = self.supervisor
                    self._generation += 1
                    self.supervisor = self._new_supervisor()
                    self._failures_since_rotation = 0
            else:
                self._consecutive_failures = 0
                self._failures_since_rotation = 0
                self._recovery_revalidation = False
                result = {
                    **result,
                    "last_success_at": result.get("sampled_at", ""),
                    "consecutive_failures": 0,
                    "stale": False,
                }
                self._last_success = dict(result)
            self._latest = result
            self._running = False
        if old_supervisor is not None:
            old_supervisor.stop()

    def invalidate_after_recovery(self, now: datetime) -> None:
        """Discard pre-recovery evidence and replace the isolated XTQuant session."""

        with self._lock:
            self._generation += 1
            self._recovery_revalidation = True
            old_supervisor = self.supervisor
            self.supervisor = self._new_supervisor()
            self._consecutive_failures = 0
            self._failures_since_rotation = 0
            if self._last_success:
                self._latest = {
                    **self._last_success,
                    "status": "stale",
                    "reason": "QMT恢复后等待业务汇总复核，暂时保留上一次有效结果",
                    "last_success_at": self._last_success.get("sampled_at", ""),
                    "last_attempt_at": now.isoformat(),
                    "consecutive_failures": 0,
                    "stale": True,
                }
            else:
                self._latest = {
                    "status": "pending",
                    "reason": "QMT恢复后等待业务汇总复核",
                    "latency_ms": 0,
                    "sampled_at": now.isoformat(),
                }
            self._next_due = now
        # A trader object that crossed a QMT process restart can reconnect yet
        # temporarily return empty collections. Destroy the whole worker so the
        # post-recovery proof always comes from a newly constructed session.
        old_supervisor.stop()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self.supervisor.stop()
