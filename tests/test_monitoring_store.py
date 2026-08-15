from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from quant_guardian.diagnostics.store import MonitoringStore


class MonitoringStoreTests(unittest.TestCase):
    @staticmethod
    def wait_for(
        callback,
        *,
        timeout: float = 2,
    ):
        deadline = time.perf_counter() + timeout
        value = callback()
        while time.perf_counter() < deadline:
            value = callback()
            if value:
                return value
            time.sleep(0.01)
        return value

    def test_events_are_filterable_and_correlated_steps_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / "logs"
            audit.mkdir()
            store = MonitoringStore(root / "monitoring.db", audit_directory=audit)
            now = datetime.now().astimezone()
            try:
                for index, event_type in enumerate(("recovery_requested", "recovery_result")):
                    store.enqueue_event(
                        {
                            "event_id": "shared-recovery-id",
                            "time": (now + timedelta(milliseconds=index)).isoformat(),
                            "event_type": event_type,
                            "severity": "warning" if index == 0 else "info",
                            "payload": {
                                "component_id": "qmt_api",
                                "reason": event_type,
                            },
                        }
                    )
                deadline = time.perf_counter() + 2
                rows = []
                while time.perf_counter() < deadline:
                    rows = store.fetch_events(component="qmt_api")
                    if len(rows) == 2:
                        break
                    time.sleep(0.01)
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["event_type"] for row in rows}, {"recovery_requested", "recovery_result"})
            finally:
                store.close()

    def test_ten_thousand_event_query_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / "logs"
            audit.mkdir()
            store = MonitoringStore(root / "monitoring.db", audit_directory=audit)
            now = datetime.now().astimezone()
            try:
                # Use the store's writer contract, then wait for the worker.
                for index in range(10_000):
                    store.enqueue_event(
                        {
                            "event_id": f"evt-{index}",
                            "time": (now + timedelta(microseconds=index)).isoformat(),
                            "event_type": "health_check",
                            "severity": "info" if index % 10 else "warning",
                            "payload": {"component_id": "qmt_api", "reason": f"sample {index}"},
                        }
                    )
                deadline = time.perf_counter() + 10
                while time.perf_counter() < deadline:
                    rows = store.fetch_events(limit=1, offset=9_999)
                    if rows:
                        break
                    time.sleep(0.02)
                started = time.perf_counter()
                rows = store.fetch_events(limit=200, severity="warning", component="qmt_api")
                elapsed_ms = (time.perf_counter() - started) * 1000
                self.assertEqual(len(rows), 200)
                self.assertLess(elapsed_ms, 150)
            finally:
                store.close()

    def test_restart_is_not_successful_until_stable_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / "logs"
            audit.mkdir()
            store = MonitoringStore(root / "monitoring.db", audit_directory=audit)
            now = datetime.now().astimezone()
            incident_id = "QGI-test"
            operation_id = "QGO-test"

            def event(event_type: str, offset: int, **payload) -> None:
                store.enqueue_event(
                    {
                        "event_id": (
                            incident_id
                            if event_type.startswith("incident_")
                            else operation_id
                        ),
                        "time": (now + timedelta(seconds=offset)).isoformat(),
                        "event_type": event_type,
                        "severity": "info",
                        "payload": payload,
                    }
                )

            try:
                event(
                    "incident_started",
                    0,
                    incident_id=incident_id,
                    component_id="qmt_api",
                    context="production",
                    status="open",
                    result="in_progress",
                    started_at=now.isoformat(),
                )
                common = {
                    "operation_id": operation_id,
                    "incident_id": incident_id,
                    "operation_type": "qmt_restart",
                    "initiator": "automatic",
                    "target_component": "qmt_api",
                    "context": "production",
                    "attempt_no": 1,
                    "started_at": now.isoformat(),
                }
                event(
                    "recovery_requested",
                    1,
                    **common,
                    status="in_progress",
                    phase="requested",
                )
                event(
                    "recovery_result",
                    2,
                    **common,
                    status="verifying",
                    phase="launch",
                    success=True,
                )
                operations = self.wait_for(
                    lambda: store.fetch_operations(status="verifying")
                )
                self.assertEqual(len(operations), 1)
                pending_stats = store.fetch_operation_stats(since=now)
                self.assertEqual(pending_stats["qmt_restart_attempts"], 1)
                self.assertEqual(pending_stats["qmt_verified_attempts"], 0)

                completed = now + timedelta(seconds=45)
                event(
                    "recovery_verified",
                    45,
                    **common,
                    status="succeeded",
                    phase="verification",
                    success=True,
                    completed_at=completed.isoformat(),
                    duration_ms=45_000,
                )
                event(
                    "incident_resolved",
                    46,
                    incident_id=incident_id,
                    component_id="qmt_api",
                    context="production",
                    status="resolved",
                    result="succeeded",
                    attempt_count=1,
                    started_at=now.isoformat(),
                    resolved_at=(now + timedelta(seconds=46)).isoformat(),
                    duration_ms=46_000,
                )
                succeeded = self.wait_for(
                    lambda: store.fetch_operations(status="succeeded")
                )
                self.assertEqual(len(succeeded), 1)
                stats = store.fetch_operation_stats(since=now)
                self.assertEqual(stats["qmt_verified_attempts"], 1)
                self.assertEqual(stats["resolved_incidents"], 1)
                self.assertEqual(stats["recovery_success_rate"], 1.0)
                detail = store.fetch_operation_detail(operation_id)
                self.assertEqual(detail["operation"]["status"], "succeeded")
                self.assertEqual(detail["incident"]["status"], "resolved")
                self.assertEqual(len(detail["events"]), 3)

                blocked_id = "QGO-blocked"
                store.enqueue_event(
                    {
                        "event_id": blocked_id,
                        "time": (now + timedelta(seconds=50)).isoformat(),
                        "event_type": "manual_qmt_restart_rejected",
                        "severity": "warning",
                        "payload": {
                            "operation_id": blocked_id,
                            "operation_type": "qmt_restart",
                            "initiator": "manual",
                            "target_component": "qmt_api",
                            "context": "production",
                            "status": "blocked",
                            "phase": "exclusive_verification",
                            "reason": "restart already verifying",
                        },
                    }
                )
                self.wait_for(
                    lambda: store.fetch_operations(status="blocked")
                )
                blocked_stats = store.fetch_operation_stats(since=now)
                self.assertEqual(blocked_stats["qmt_restart_attempts"], 1)
                self.assertEqual(blocked_stats["blocked_operations"], 1)
            finally:
                store.close()

    def test_legacy_backfill_groups_a_repeated_recovery_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / "logs"
            audit.mkdir()
            now = datetime.now().astimezone() - timedelta(hours=1)
            records: list[dict[str, object]] = []

            def add(
                event_id: str,
                event_type: str,
                seconds: int,
                payload: dict[str, object],
            ) -> None:
                records.append(
                    {
                        "event_id": event_id,
                        "time": (now + timedelta(seconds=seconds)).isoformat(),
                        "event_type": event_type,
                        "severity": "info",
                        "payload": payload,
                    }
                )

            for operation_id, seconds in (("legacy-a", 0), ("legacy-b", 120)):
                add(
                    operation_id,
                    "recovery_requested",
                    seconds,
                    {"component_id": "qmt_api", "reason": "restart requested"},
                )
                add(
                    operation_id,
                    "recovery_result",
                    seconds + 1,
                    {
                        "component_id": "qmt_api",
                        "success": True,
                        "reason": "launcher returned successfully",
                    },
                )
            add(
                "transition-stable",
                "state_transition",
                165,
                {
                    "component_id": "qmt_api",
                    "reason": "stable health verification completed",
                },
            )
            path = audit / f"guardian-{now:%Y%m%d}.jsonl"
            path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            store = MonitoringStore(root / "monitoring.db", audit_directory=audit)
            try:
                operations = self.wait_for(
                    lambda: store.fetch_operations(
                        operation_type="qmt_restart", context="legacy"
                    )
                )
                self.assertEqual(len(operations), 2)
                self.assertEqual(
                    {row["attempt_no"] for row in operations}, {1, 2}
                )
                self.assertEqual(
                    len({row["incident_id"] for row in operations}), 1
                )
                stats = store.fetch_operation_stats(since=now, context="legacy")
                self.assertEqual(stats["qmt_restart_attempts"], 2)
                self.assertEqual(stats["qmt_verified_attempts"], 1)
                self.assertEqual(stats["recovery_incidents"], 1)
                self.assertEqual(stats["resolved_incidents"], 1)
                self.assertEqual(stats["repeated_incidents"], 1)
                failed_operation = next(
                    row for row in operations if row["status"] == "failed"
                )
                succeeded_operation = next(
                    row for row in operations if row["status"] == "succeeded"
                )
                failed_detail = store.fetch_operation_detail(
                    failed_operation["operation_id"]
                )
                succeeded_detail = store.fetch_operation_detail(
                    succeeded_operation["operation_id"]
                )
                self.assertIn(
                    "legacy_verification_inferred_failed",
                    {event["event_type"] for event in failed_detail["events"]},
                )
                self.assertIn(
                    "legacy_stable_verification",
                    {
                        event["event_type"]
                        for event in succeeded_detail["events"]
                    },
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
