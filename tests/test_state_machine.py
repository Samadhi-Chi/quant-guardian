from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from quant_guardian.domain.models import (
    GuardianState,
    HealthSnapshot,
    LogSignal,
    ProbeStatus,
    ProcessStatus,
    RecommendedAction,
)
from quant_guardian.domain.state_machine import (
    GuardianStateMachine,
    StateMachinePolicy,
)

BASE = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def snapshot(
    offset: int,
    *,
    process: ProcessStatus = ProcessStatus.HEALTHY,
    probe: ProbeStatus = ProbeStatus.HEALTHY,
    log: LogSignal = LogSignal.POSITIVE,
    network: bool = True,
    manual: bool = False,
) -> HealthSnapshot:
    return HealthSnapshot(
        observed_at=BASE + timedelta(seconds=offset),
        process_status=process,
        probe_status=probe,
        log_signal=log,
        network_available=network,
        login_requires_manual=manual,
    )


def policy() -> StateMachinePolicy:
    return StateMachinePolicy(
        failure_threshold=3,
        failure_window_seconds=45,
        startup_grace_seconds=0,
        resume_grace_seconds=0,
        verify_successes=2,
        verify_min_span_seconds=1,
        max_attempts_per_30_minutes=3,
        max_attempts_per_day=5,
        backoff_seconds=(0, 0, 0),
    )


class StateMachineTests(unittest.TestCase):
    def test_stable_samples_are_required_for_healthy(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        first = machine.observe(snapshot(0), recovery_permitted=False)
        second = machine.observe(snapshot(1), recovery_permitted=False)
        self.assertEqual(first.new_state, GuardianState.STARTING)
        self.assertEqual(second.new_state, GuardianState.HEALTHY)

    def test_verification_span_can_exceed_required_sample_count(self) -> None:
        long_policy = replace(
            policy(),
            verify_successes=3,
            verify_min_span_seconds=30,
        )
        machine = GuardianStateMachine(long_policy, now=BASE)
        for offset in range(0, 30, 5):
            result = machine.observe(snapshot(offset), recovery_permitted=False)
            self.assertEqual(result.new_state, GuardianState.STARTING)
        result = machine.observe(snapshot(30), recovery_permitted=False)
        self.assertEqual(result.new_state, GuardianState.HEALTHY)


    def test_transient_failure_does_not_recover(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        machine.observe(snapshot(0), recovery_permitted=True)
        machine.observe(snapshot(1), recovery_permitted=True)
        result = machine.observe(
            snapshot(2, probe=ProbeStatus.TIMEOUT, log=LogSignal.NEUTRAL),
            recovery_permitted=True,
        )
        self.assertEqual(result.new_state, GuardianState.SUSPECT)
        self.assertEqual(result.action, RecommendedAction.WAIT)

    def test_three_failures_request_recovery(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        machine.observe(snapshot(0), recovery_permitted=True)
        machine.observe(snapshot(1), recovery_permitted=True)
        for offset in (2, 7):
            machine.observe(
                snapshot(offset, probe=ProbeStatus.FAILED),
                recovery_permitted=True,
            )
        result = machine.observe(
            snapshot(12, probe=ProbeStatus.FAILED),
            recovery_permitted=True,
        )
        self.assertEqual(result.action, RecommendedAction.RECOVER_QMT)

    def test_missing_process_requires_three_consistent_failures(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        results = []
        for offset in (1, 2, 3):
            results.append(
                machine.observe(
                    snapshot(
                        offset,
                        process=ProcessStatus.MISSING,
                        probe=ProbeStatus.FAILED,
                        log=LogSignal.STALE,
                    ),
                    recovery_permitted=True,
                )
            )
        self.assertEqual(results[0].action, RecommendedAction.WAIT)
        self.assertEqual(results[1].action, RecommendedAction.WAIT)
        result = results[2]
        self.assertEqual(result.action, RecommendedAction.RECOVER_QMT)

    def test_observation_mode_never_requests_live_action(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        result = None
        for offset in (1, 2, 3):
            result = machine.observe(
                snapshot(
                    offset,
                    process=ProcessStatus.MISSING,
                    probe=ProbeStatus.FAILED,
                ),
                recovery_permitted=False,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.new_state, GuardianState.DEGRADED)
        self.assertEqual(result.action, RecommendedAction.NONE)

    def test_confirmed_failure_can_require_manual_action_when_recovery_is_blocked(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        result = None
        for offset in (1, 2, 3):
            result = machine.observe(
                snapshot(
                    offset,
                    process=ProcessStatus.MISSING,
                    probe=ProbeStatus.FAILED,
                ),
                recovery_permitted=False,
                recovery_block_reason="Rocket is active; operator intervention required",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.new_state, GuardianState.MANUAL_REQUIRED)
        self.assertEqual(result.action, RecommendedAction.REQUIRE_MANUAL)
        self.assertIn("Rocket is active", result.reason)

    def test_network_failure_suppresses_recovery(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        result = machine.observe(
            snapshot(
                1,
                process=ProcessStatus.MISSING,
                probe=ProbeStatus.FAILED,
                network=False,
            ),
            recovery_permitted=True,
        )
        self.assertEqual(result.action, RecommendedAction.WAIT_NETWORK)

    def test_repeated_failed_recovery_locks_out(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        for attempt, offset in enumerate((1, 10, 20), start=1):
            decision = None
            for sample_offset in (offset, offset + 1, offset + 2):
                decision = machine.observe(
                    snapshot(
                        sample_offset,
                        process=ProcessStatus.MISSING,
                        probe=ProbeStatus.FAILED,
                    ),
                    recovery_permitted=True,
                )
            self.assertIsNotNone(decision)
            self.assertEqual(decision.action, RecommendedAction.RECOVER_QMT)
            machine.mark_recovery_started(BASE + timedelta(seconds=offset + 2))
            failed = machine.mark_recovery_failed(
                BASE + timedelta(seconds=offset + 3),
                f"failure {attempt}",
            )
        self.assertEqual(failed.new_state, GuardianState.LOCKOUT)

    def test_recovery_verification_is_exclusive_until_deadline(self) -> None:
        guarded = replace(
            policy(),
            startup_grace_seconds=2,
            verification_timeout_seconds=10,
            backoff_seconds=(5,),
        )
        machine = GuardianStateMachine(guarded, now=BASE)
        for offset in (3, 4, 5):
            decision = machine.observe(
                snapshot(offset, probe=ProbeStatus.TIMEOUT),
                recovery_permitted=True,
            )
        self.assertEqual(decision.action, RecommendedAction.RECOVER_QMT)
        machine.mark_recovery_started(BASE + timedelta(seconds=5))
        machine.mark_launch_succeeded(BASE + timedelta(seconds=6))

        for offset in (9, 11, 14):
            waiting = machine.observe(
                snapshot(offset, probe=ProbeStatus.TIMEOUT),
                recovery_permitted=True,
            )
            self.assertEqual(waiting.new_state, GuardianState.VERIFYING)
            self.assertEqual(waiting.action, RecommendedAction.WAIT)

        failed = machine.observe(
            snapshot(16, probe=ProbeStatus.TIMEOUT),
            recovery_permitted=True,
        )
        self.assertEqual(failed.new_state, GuardianState.DEGRADED)
        self.assertEqual(failed.action, RecommendedAction.WAIT)
        self.assertIn("verification failed", failed.reason)
        self.assertEqual(
            machine.next_attempt_at,
            BASE + timedelta(seconds=21),
        )

    def test_sparse_healthy_samples_cannot_extend_verification_forever(self) -> None:
        guarded = replace(
            policy(),
            verify_successes=3,
            verify_min_span_seconds=30,
            verification_timeout_seconds=10,
            backoff_seconds=(5,),
        )
        machine = GuardianStateMachine(guarded, now=BASE)
        machine.mark_recovery_started(BASE)
        machine.mark_launch_succeeded(BASE + timedelta(seconds=1))
        waiting = machine.observe(snapshot(7), recovery_permitted=True)
        self.assertEqual(waiting.new_state, GuardianState.VERIFYING)
        timed_out = machine.observe(snapshot(11), recovery_permitted=True)
        self.assertEqual(timed_out.new_state, GuardianState.DEGRADED)
        self.assertIn("verification failed", timed_out.reason)

    def test_manual_login_is_never_automated(self) -> None:
        machine = GuardianStateMachine(policy(), now=BASE)
        result = machine.observe(
            snapshot(1, probe=ProbeStatus.FAILED, manual=True),
            recovery_permitted=True,
        )
        self.assertEqual(result.new_state, GuardianState.MANUAL_REQUIRED)
        self.assertEqual(result.action, RecommendedAction.REQUIRE_MANUAL)


if __name__ == "__main__":
    unittest.main()
