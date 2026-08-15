from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from quant_guardian.config import AppConfig
from quant_guardian.domain.models import (
    GuardianState,
    HealthSnapshot,
    RecommendedAction,
    Transition,
)


@dataclass(frozen=True, slots=True)
class StateMachinePolicy:
    failure_threshold: int = 3
    failure_window_seconds: int = 45
    startup_grace_seconds: int = 90
    resume_grace_seconds: int = 120
    verify_successes: int = 3
    verify_min_span_seconds: int = 30
    verification_timeout_seconds: int = 180
    max_attempts_per_30_minutes: int = 3
    max_attempts_per_day: int = 5
    backoff_seconds: tuple[int, ...] = (60, 120, 300, 600)

    @classmethod
    def from_config(cls, config: AppConfig) -> StateMachinePolicy:
        return cls(
            failure_threshold=config.thresholds.failure_threshold,
            failure_window_seconds=config.thresholds.failure_window_seconds,
            startup_grace_seconds=config.thresholds.startup_grace_seconds,
            resume_grace_seconds=config.thresholds.resume_grace_seconds,
            verify_successes=config.thresholds.verify_successes,
            verify_min_span_seconds=config.thresholds.verify_min_span_seconds,
            verification_timeout_seconds=(
                config.thresholds.verification_timeout_seconds
            ),
            max_attempts_per_30_minutes=(
                config.recovery.max_attempts_per_30_minutes
            ),
            max_attempts_per_day=config.recovery.max_attempts_per_day,
            backoff_seconds=tuple(config.recovery.backoff_seconds),
        )


class GuardianStateMachine:
    """Pure, deterministic state machine for health and recovery decisions."""

    def __init__(
        self,
        policy: StateMachinePolicy,
        *,
        now: datetime | None = None,
    ) -> None:
        started = now or datetime.now().astimezone()
        self.policy = policy
        self.state = GuardianState.STARTING
        self.grace_until = started + timedelta(seconds=policy.startup_grace_seconds)
        self.next_attempt_at: datetime | None = None
        self.verification_deadline: datetime | None = None
        self.verification_is_manual = False
        self.last_snapshot: HealthSnapshot | None = None
        self.last_transition: Transition | None = None
        self._failure_times: deque[datetime] = deque()
        self._success_times: deque[datetime] = deque()
        self._attempt_times: deque[datetime] = deque()
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def recovery_attempt_count(self) -> int:
        return len(self._attempt_times)

    def _transition(
        self,
        now: datetime,
        new_state: GuardianState,
        action: RecommendedAction,
        reason: str,
        snapshot: HealthSnapshot | None = None,
    ) -> Transition:
        transition = Transition(
            at=now,
            old_state=self.state,
            new_state=new_state,
            action=action,
            reason=reason,
            snapshot=snapshot,
        )
        self.state = new_state
        self.last_transition = transition
        return transition

    def _prune_failures(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.policy.failure_window_seconds)
        while self._failure_times and self._failure_times[0] < cutoff:
            self._failure_times.popleft()

    def _prune_attempts(self, now: datetime) -> None:
        day_cutoff = now - timedelta(days=1)
        while self._attempt_times and self._attempt_times[0] < day_cutoff:
            self._attempt_times.popleft()

    def _attempts_in_last_30_minutes(self, now: datetime) -> int:
        cutoff = now - timedelta(minutes=30)
        return sum(attempt >= cutoff for attempt in self._attempt_times)

    def _verification_complete(self) -> bool:
        if len(self._success_times) < self.policy.verify_successes:
            return False
        span = (self._success_times[-1] - self._success_times[0]).total_seconds()
        return span >= self.policy.verify_min_span_seconds

    def observe(
        self,
        snapshot: HealthSnapshot,
        *,
        recovery_permitted: bool,
        recovery_block_reason: str | None = None,
    ) -> Transition:
        now = snapshot.observed_at
        self.last_snapshot = snapshot

        if self._paused:
            return self._transition(
                now,
                GuardianState.PAUSED,
                RecommendedAction.NONE,
                "automatic recovery is paused",
                snapshot,
            )

        if self.state is GuardianState.LOCKOUT:
            return self._transition(
                now,
                GuardianState.LOCKOUT,
                RecommendedAction.LOCKOUT,
                "recovery is locked until explicit user unlock",
                snapshot,
            )

        if snapshot.login_requires_manual:
            self._failure_times.clear()
            self._success_times.clear()
            return self._transition(
                now,
                GuardianState.MANUAL_REQUIRED,
                RecommendedAction.REQUIRE_MANUAL,
                "QMT login requires password, captcha, or user confirmation",
                snapshot,
            )

        if not snapshot.network_available:
            self._failure_times.clear()
            self._success_times.clear()
            state = (
                GuardianState.STARTING
                if self.state is GuardianState.STARTING
                else GuardianState.SUSPECT
            )
            return self._transition(
                now,
                state,
                RecommendedAction.WAIT_NETWORK,
                "network is unavailable; QMT restart is suppressed",
                snapshot,
            )

        if snapshot.is_healthy:
            self._failure_times.clear()
            if self.state in {
                GuardianState.STARTING,
                GuardianState.RECOVERING,
                GuardianState.VERIFYING,
                GuardianState.MANUAL_REQUIRED,
                GuardianState.DEGRADED,
                GuardianState.SUSPECT,
            }:
                self._success_times.append(now)
                if self._verification_complete():
                    self._success_times.clear()
                    self.verification_deadline = None
                    self.verification_is_manual = False
                    self.next_attempt_at = None
                    return self._transition(
                        now,
                        GuardianState.HEALTHY,
                        RecommendedAction.NONE,
                        "stable health verification completed",
                        snapshot,
                    )
                if (
                    self.state is GuardianState.VERIFYING
                    and self.verification_deadline is not None
                    and now >= self.verification_deadline
                ):
                    return self._verification_timed_out(now, snapshot)
                target = (
                    GuardianState.STARTING
                    if self.state is GuardianState.STARTING
                    else GuardianState.VERIFYING
                )
                return self._transition(
                    now,
                    target,
                    RecommendedAction.VERIFY,
                    "healthy sample received; stable verification continues",
                    snapshot,
                )
            self._success_times.clear()
            return self._transition(
                now,
                GuardianState.HEALTHY,
                RecommendedAction.NONE,
                "health check passed",
                snapshot,
            )

        self._success_times.clear()

        if (
            self.state in {GuardianState.STARTING, GuardianState.VERIFYING}
            and now < self.grace_until
        ):
            return self._transition(
                now,
                self.state,
                RecommendedAction.WAIT,
                "failure occurred inside startup or resume grace period",
                snapshot,
            )

        if (
            self.state is GuardianState.VERIFYING
            and self.verification_deadline is not None
        ):
            if now < self.verification_deadline:
                return self._transition(
                    now,
                    GuardianState.VERIFYING,
                    RecommendedAction.WAIT,
                    "recovery verification is still in progress; unhealthy sample recorded",
                    snapshot,
                )
            return self._verification_timed_out(now, snapshot)

        self._failure_times.append(now)
        self._prune_failures(now)

        threshold_met = len(self._failure_times) >= self.policy.failure_threshold

        if not threshold_met:
            return self._transition(
                now,
                GuardianState.SUSPECT,
                RecommendedAction.WAIT,
                f"failure sample {len(self._failure_times)} of "
                f"{self.policy.failure_threshold}; waiting for confirmation",
                snapshot,
            )

        if not recovery_permitted:
            if recovery_block_reason:
                return self._transition(
                    now,
                    GuardianState.MANUAL_REQUIRED,
                    RecommendedAction.REQUIRE_MANUAL,
                    recovery_block_reason,
                    snapshot,
                )
            return self._transition(
                now,
                GuardianState.DEGRADED,
                RecommendedAction.NONE,
                "fault confirmed; observation mode prevents live recovery",
                snapshot,
            )

        self._prune_attempts(now)
        if self.next_attempt_at and now < self.next_attempt_at:
            return self._transition(
                now,
                GuardianState.DEGRADED,
                RecommendedAction.WAIT,
                f"recovery backoff active until {self.next_attempt_at.isoformat()}",
                snapshot,
            )

        if (
            self._attempts_in_last_30_minutes(now)
            >= self.policy.max_attempts_per_30_minutes
            or len(self._attempt_times) >= self.policy.max_attempts_per_day
        ):
            return self._transition(
                now,
                GuardianState.LOCKOUT,
                RecommendedAction.LOCKOUT,
                "recovery attempt limit reached",
                snapshot,
            )

        return self._transition(
            now,
            GuardianState.DEGRADED,
            RecommendedAction.RECOVER_QMT,
            "fault confirmed and recovery policy permits QMT restart",
            snapshot,
        )

    def mark_recovery_started(self, now: datetime) -> Transition:
        self._prune_attempts(now)
        self._attempt_times.append(now)
        self._failure_times.clear()
        self._success_times.clear()
        self.verification_deadline = None
        self.verification_is_manual = False
        return self._transition(
            now,
            GuardianState.RECOVERING,
            RecommendedAction.WAIT,
            "controlled QMT recovery started",
            self.last_snapshot,
        )

    def mark_manual_restart_started(self, now: datetime) -> Transition:
        """Enter recovery for a confirmed operator action without retry accounting."""

        self._failure_times.clear()
        self._success_times.clear()
        self.verification_deadline = None
        self.verification_is_manual = True
        return self._transition(
            now,
            GuardianState.RECOVERING,
            RecommendedAction.WAIT,
            "operator-confirmed QMT restart started",
            self.last_snapshot,
        )

    def mark_launch_succeeded(
        self,
        now: datetime,
        *,
        manual: bool = False,
    ) -> Transition:
        self.grace_until = now + timedelta(seconds=self.policy.startup_grace_seconds)
        self.verification_deadline = now + timedelta(
            seconds=self.policy.verification_timeout_seconds
        )
        self.verification_is_manual = manual
        self._success_times.clear()
        return self._transition(
            now,
            GuardianState.VERIFYING,
            RecommendedAction.VERIFY,
            "QMT launch completed; waiting for stable business verification",
            self.last_snapshot,
        )

    def _verification_failed(self, now: datetime, reason: str) -> Transition:
        """Finish an unsuccessful automatic attempt before any new retry."""

        self._prune_attempts(now)
        recent = self._attempts_in_last_30_minutes(now)
        if (
            recent >= self.policy.max_attempts_per_30_minutes
            or len(self._attempt_times) >= self.policy.max_attempts_per_day
        ):
            return self._transition(
                now,
                GuardianState.LOCKOUT,
                RecommendedAction.LOCKOUT,
                f"recovery verification failed and attempt limit was reached: {reason}",
                self.last_snapshot,
            )
        index = max(
            0,
            min(
                len(self._attempt_times) - 1,
                len(self.policy.backoff_seconds) - 1,
            ),
        )
        delay = self.policy.backoff_seconds[index] if self.policy.backoff_seconds else 60
        self.next_attempt_at = now + timedelta(seconds=delay)
        self._failure_times.clear()
        return self._transition(
            now,
            GuardianState.DEGRADED,
            RecommendedAction.WAIT,
            f"recovery verification failed; next attempt after {delay} seconds: {reason}",
            self.last_snapshot,
        )

    def _verification_timed_out(
        self,
        now: datetime,
        snapshot: HealthSnapshot,
    ) -> Transition:
        deadline = self.verification_deadline
        self.verification_deadline = None
        if self.verification_is_manual:
            self.verification_is_manual = False
            return self._transition(
                now,
                GuardianState.MANUAL_REQUIRED,
                RecommendedAction.REQUIRE_MANUAL,
                "operator-confirmed QMT restart did not pass stable verification "
                f"before {deadline.isoformat() if deadline else 'the deadline'}",
                snapshot,
            )
        return self._verification_failed(
            now,
            "QMT restart did not pass stable verification before "
            f"{deadline.isoformat() if deadline else 'the deadline'}",
        )

    def mark_recovery_failed(self, now: datetime, reason: str) -> Transition:
        self.verification_deadline = None
        self.verification_is_manual = False
        self._prune_attempts(now)
        recent = self._attempts_in_last_30_minutes(now)
        if (
            recent >= self.policy.max_attempts_per_30_minutes
            or len(self._attempt_times) >= self.policy.max_attempts_per_day
        ):
            return self._transition(
                now,
                GuardianState.LOCKOUT,
                RecommendedAction.LOCKOUT,
                f"recovery failed and attempt limit was reached: {reason}",
                self.last_snapshot,
            )
        index = max(0, min(len(self._attempt_times) - 1, len(self.policy.backoff_seconds) - 1))
        delay = self.policy.backoff_seconds[index] if self.policy.backoff_seconds else 60
        self.next_attempt_at = now + timedelta(seconds=delay)
        return self._transition(
            now,
            GuardianState.DEGRADED,
            RecommendedAction.WAIT,
            f"recovery failed; next attempt after {delay} seconds: {reason}",
            self.last_snapshot,
        )

    def mark_manual_restart_failed(self, now: datetime, reason: str) -> Transition:
        """Report a manual failure without consuming automatic retry budgets."""

        self.verification_deadline = None
        self.verification_is_manual = False

        return self._transition(
            now,
            GuardianState.DEGRADED,
            RecommendedAction.NONE,
            f"operator-confirmed QMT restart failed: {reason}",
            self.last_snapshot,
        )

    def mark_manual_required(self, now: datetime, reason: str) -> Transition:
        return self._transition(
            now,
            GuardianState.MANUAL_REQUIRED,
            RecommendedAction.REQUIRE_MANUAL,
            reason,
            self.last_snapshot,
        )

    def pause(self, now: datetime) -> Transition:
        self._paused = True
        return self._transition(
            now,
            GuardianState.PAUSED,
            RecommendedAction.NONE,
            "automatic recovery paused by user",
            self.last_snapshot,
        )

    def resume(self, now: datetime) -> Transition:
        self._paused = False
        self.grace_until = now + timedelta(seconds=self.policy.resume_grace_seconds)
        self._failure_times.clear()
        self._success_times.clear()
        self.verification_deadline = None
        self.verification_is_manual = False
        return self._transition(
            now,
            GuardianState.STARTING,
            RecommendedAction.VERIFY,
            "automatic recovery resumed; grace period started",
            self.last_snapshot,
        )

    def unlock(self, now: datetime) -> Transition:
        self._attempt_times.clear()
        self.next_attempt_at = None
        self.verification_deadline = None
        self.verification_is_manual = False
        self.grace_until = now + timedelta(seconds=self.policy.startup_grace_seconds)
        return self._transition(
            now,
            GuardianState.STARTING,
            RecommendedAction.VERIFY,
            "recovery lockout cleared by user",
            self.last_snapshot,
        )

    def acknowledge_manual(self, now: datetime) -> Transition:
        if self.state is not GuardianState.MANUAL_REQUIRED:
            return self._transition(
                now,
                self.state,
                RecommendedAction.NONE,
                "manual acknowledgement ignored because no acknowledgement is pending",
                self.last_snapshot,
            )
        if self.last_snapshot and self.last_snapshot.is_healthy:
            return self._transition(
                now,
                GuardianState.HEALTHY,
                RecommendedAction.NONE,
                "manual reconciliation acknowledgement recorded",
                self.last_snapshot,
            )
        self.grace_until = now + timedelta(seconds=self.policy.resume_grace_seconds)
        return self._transition(
            now,
            GuardianState.STARTING,
            RecommendedAction.VERIFY,
            "manual acknowledgement recorded; fresh health verification is required",
            self.last_snapshot,
        )
    def note_system_resume(self, now: datetime) -> Transition:
        self.grace_until = now + timedelta(seconds=self.policy.resume_grace_seconds)
        self._failure_times.clear()
        self._success_times.clear()
        self.verification_deadline = None
        self.verification_is_manual = False
        return self._transition(
            now,
            GuardianState.STARTING,
            RecommendedAction.WAIT,
            "system resumed; temporary grace period started",
            self.last_snapshot,
        )
