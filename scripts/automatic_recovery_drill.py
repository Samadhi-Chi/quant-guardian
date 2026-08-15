from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import psutil  # noqa: E402

from quant_guardian.config import load_config  # noqa: E402
from quant_guardian.domain.trading_calendar import TradingCalendar  # noqa: E402
from quant_guardian.monitors.process_monitor import (  # noqa: E402
    ProcessIdentity,
    QmtProcessMonitor,
)
from quant_guardian.recovery.windows_process_control import (  # noqa: E402
    request_graceful_close,
    terminate_exact,
    wait_for_exit,
)
from quant_guardian.safety import SafetyGate  # noqa: E402
from quant_guardian.service import GuardianService  # noqa: E402

CONFIRMATION = "AUTOMATIC-QMT-RECOVERY-DRILL"
COUNT_KEYS = ("orders", "cancelable_orders", "trades", "positions")


def _path_key(value: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(value).resolve(strict=False)))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(str(value)))


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _qmt_processes(monitor: QmtProcessMonitor) -> list[dict[str, object]]:
    return [
        {
            "pid": item.pid,
            "name": item.name,
            "executable": item.executable,
            "started_at_epoch": item.create_time,
        }
        for item in sorted(
            monitor.validated_processes(), key=lambda value: (value.name, value.pid)
        )
    ]


def _main_pids(items: list[dict[str, object]]) -> set[int]:
    return {
        int(item["pid"])
        for item in items
        if str(item["name"]).casefold() == "xtminiqmt.exe"
    }


def _exact_process_pids(executable: Path) -> set[int]:
    expected = _path_key(executable)
    found: set[int] = set()
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            if _path_key(str(process.info.get("exe") or "")) == expected:
                found.add(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, RuntimeError):
            continue
    return found


def _business_counts(summary: dict[str, object]) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for key in COUNT_KEYS:
        value = summary.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        values[key] = value
    return values


def _installed_guardian_pids() -> set[int]:
    expected = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Quant Guardian" / "Quant Guardian.exe"
    return _exact_process_pids(expected)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one real after-hours automatic QMT recovery drill."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != CONFIRMATION:
        raise SystemExit(f"Refusing live action: --confirm must equal {CONFIRMATION}")
    if _installed_guardian_pids():
        raise SystemExit(
            "Refusing live action: installed Quant Guardian is running; stop it to avoid competing recovery loops"
        )

    config_path = args.config.resolve(strict=True)
    config = load_config(config_path)
    now = datetime.now().astimezone()
    schedule = TradingCalendar(config.trading, config.monitoring).schedule_at(now)
    gate = SafetyGate(config).status(now)
    if schedule.mode != "idle":
        raise SystemExit("Refusing live action: automatic drill is restricted to idle hours")
    if not config.monitoring.allow_idle_recovery:
        raise SystemExit("Refusing live action: idle recovery is disabled")
    if not gate.live_actions_allowed:
        raise SystemExit(f"Refusing live action: safety gate is closed ({gate.reason})")
    if config.recovery.allow_qmt_restart_while_rocket_active:
        raise SystemExit(
            "Refusing live action: Rocket-active automatic recovery suppression is not enabled"
        )

    monitor = QmtProcessMonitor(config.qmt)
    before_qmt = _qmt_processes(monitor)
    before_main = _main_pids(before_qmt)
    if not before_main:
        raise SystemExit("Refusing live action: no validated XtMiniQmt process exists")
    quantclass_executable = Path(config.trade_system.client_executable)
    before_quantclass = _exact_process_pids(quantclass_executable)
    if not before_quantclass:
        raise SystemExit("Refusing live action: no exact QuantClass client process exists")

    report: dict[str, Any] = {
        "schema_version": 1,
        "drill_type": "real_automatic_qmt_recovery",
        "started_at": now.isoformat(),
        "schedule_mode": schedule.mode,
        "safety_gate_open": True,
        "rocket_active_recovery_allowed": False,
        "before": {
            "qmt_processes": before_qmt,
            "quantclass_pids": sorted(before_quantclass),
        },
        "failure_samples": [],
        "verification_samples": [],
        "success": False,
    }
    _write_report(args.output, report)

    service = GuardianService(config, process_monitor=monitor, now=now)
    fault_injected = False
    try:
        baseline_budget = max(
            120,
            config.thresholds.startup_grace_seconds
            + config.thresholds.verify_min_span_seconds
            + 30,
        )
        baseline_deadline = time.monotonic() + baseline_budget
        baseline_status = service.run_once()
        baseline_counts: dict[str, int] | None = None
        while time.monotonic() < baseline_deadline:
            baseline_counts = _business_counts(baseline_status.business_summary)
            if (
                baseline_status.state.value == "healthy"
                and baseline_status.components.get("qmt_api", {}).get("state")
                == "healthy"
                and baseline_status.probe.get("status") == "healthy"
                and baseline_counts is not None
                and not bool(baseline_status.rocket.get("active"))
            ):
                break
            if bool(baseline_status.rocket.get("active")):
                raise RuntimeError(
                    "Rocket is active; the drill stopped before injecting a QMT fault"
                )
            time.sleep(3)
            baseline_status = service.run_once()
        else:
            raise RuntimeError(
                "baseline QMT API, business summary, or Rocket-idle state did not become ready"
            )

        report["before"].update(
            {
                "qmt_api_state": baseline_status.components["qmt_api"]["state"],
                "probe_status": baseline_status.probe.get("status"),
                "business_status": baseline_status.business_summary.get("status"),
                "business_counts": baseline_counts,
                "rocket_active": baseline_status.rocket.get("active"),
            }
        )
        _write_report(args.output, report)
        print("baseline_verified", flush=True)

        identities: tuple[ProcessIdentity, ...] = monitor.validated_processes()
        main_pids = {
            item.pid
            for item in identities
            if item.name.casefold() == "xtminiqmt.exe"
        }
        request_graceful_close(main_pids)
        remaining = wait_for_exit(identities, timeout=5.0)
        if remaining:
            terminate_exact(remaining, Path(config.qmt.launcher).parent)
            remaining = wait_for_exit(remaining, timeout=5.0)
        if remaining or _main_pids(_qmt_processes(monitor)):
            raise RuntimeError("failed to inject an exact QMT process-stop fault")
        fault_injected = True
        report["fault_injected_at"] = datetime.now().astimezone().isoformat()
        report["fault_injection"] = {
            "validated_pids": [item.pid for item in identities],
            "quantclass_pids_unchanged": (
                _exact_process_pids(quantclass_executable) == before_quantclass
            ),
        }
        _write_report(args.output, report)
        print("qmt_fault_injected", flush=True)

        recovery_started = False
        for sample_number in range(1, config.monitoring.anomaly_confirmation_checks + 1):
            status = service.run_once()
            sample = {
                "sample": sample_number,
                "at": datetime.now().astimezone().isoformat(),
                "guardian_state": status.state.value,
                "action": status.action.value,
                "reason": status.reason,
                "quantclass_pids_unchanged": (
                    _exact_process_pids(quantclass_executable) == before_quantclass
                ),
            }
            report["failure_samples"].append(sample)
            _write_report(args.output, report)
            print(json.dumps(sample, ensure_ascii=False), flush=True)
            if status.state.value in {"recovering", "verifying"}:
                recovery_started = True
                break
            if sample_number < config.monitoring.anomaly_confirmation_checks:
                time.sleep(config.monitoring.anomaly_retry_seconds)
        if not recovery_started:
            raise RuntimeError("three automatic failure samples did not start QMT recovery")

        deadline = time.monotonic() + args.timeout
        final_status = status
        final_qmt: list[dict[str, object]] = []
        final_counts: dict[str, int] | None = None
        while time.monotonic() < deadline:
            time.sleep(5)
            final_status = service.run_once()
            final_qmt = _qmt_processes(monitor)
            final_main = _main_pids(final_qmt)
            final_counts = _business_counts(final_status.business_summary)
            quantclass_unchanged = (
                _exact_process_pids(quantclass_executable) == before_quantclass
            )
            sample = {
                "at": datetime.now().astimezone().isoformat(),
                "guardian_state": final_status.state.value,
                "qmt_api_state": final_status.components.get("qmt_api", {}).get("state"),
                "probe_status": final_status.probe.get("status"),
                "business_status": final_status.business_summary.get("status"),
                "new_qmt_main_pid_seen": bool(
                    final_main and final_main.isdisjoint(before_main)
                ),
                "quantclass_pids_unchanged": quantclass_unchanged,
            }
            report["verification_samples"] = (
                report["verification_samples"] + [sample]
            )[-18:]
            _write_report(args.output, report)
            print(json.dumps(sample, ensure_ascii=False), flush=True)
            if not quantclass_unchanged:
                raise RuntimeError("QuantClass process IDs changed during QMT recovery")
            if (
                final_status.state.value == "healthy"
                and final_status.components.get("qmt_api", {}).get("state") == "healthy"
                and final_status.probe.get("status") == "healthy"
                and final_counts is not None
                and final_main
                and final_main.isdisjoint(before_main)
            ):
                break
        else:
            raise RuntimeError("automatic QMT recovery did not verify before timeout")

        events = service.audit.recent(100)
        recovery_events = [
            event
            for event in events
            if event.get("event_type") in {"recovery_requested", "recovery_result"}
        ]
        if not any(
            event.get("event_type") == "recovery_result"
            and event.get("payload", {}).get("success") is True
            for event in recovery_events
        ):
            raise RuntimeError("successful automatic recovery audit event was not found")
        if any(
            event.get("event_type", "").startswith("manual_quantclass_restart")
            for event in events
        ):
            raise RuntimeError("unexpected QuantClass restart audit event was recorded")

        report["success"] = True
        report["completed_at"] = datetime.now().astimezone().isoformat()
        report["after"] = {
            "qmt_processes": final_qmt,
            "quantclass_pids": sorted(
                _exact_process_pids(quantclass_executable)
            ),
            "quantclass_pids_unchanged": True,
            "guardian_state": final_status.state.value,
            "qmt_api_state": final_status.components["qmt_api"]["state"],
            "probe_status": final_status.probe.get("status"),
            "business_status": final_status.business_summary.get("status"),
            "business_counts": final_counts,
            "recovery_event_types": [
                str(event.get("event_type")) for event in recovery_events
            ],
        }
        _write_report(args.output, report)
        print(f"automatic_recovery_passed report={args.output}", flush=True)
        return 0
    except Exception as exc:
        report["failed_at"] = datetime.now().astimezone().isoformat()
        report["failure"] = f"{type(exc).__name__}: {exc}"
        report["fault_was_injected"] = fault_injected
        _write_report(args.output, report)
        raise
    finally:
        service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
