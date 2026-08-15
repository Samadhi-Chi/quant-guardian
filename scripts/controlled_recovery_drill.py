from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_guardian.config import load_config  # noqa: E402
from quant_guardian.domain.trading_calendar import TradingCalendar  # noqa: E402
from quant_guardian.monitors.process_monitor import QmtProcessMonitor  # noqa: E402
from quant_guardian.safety import SafetyGate  # noqa: E402
from quant_guardian.service import GuardianService  # noqa: E402

CONFIRMATION = "CONTROLLED-QMT-RESTART"
COUNT_KEYS = ("orders", "cancelable_orders", "trades", "positions")


def _identities(monitor: QmtProcessMonitor) -> list[dict[str, object]]:
    return [
        {
            "pid": item.pid,
            "name": item.name,
            "started_at_epoch": item.create_time,
            "responsive": item.responsive,
        }
        for item in sorted(monitor.validated_processes(), key=lambda value: value.name)
    ]


def _main_pids(items: list[dict[str, object]]) -> set[int]:
    return {
        int(item["pid"])
        for item in items
        if str(item["name"]).casefold() == "xtminiqmt.exe"
    }


def _business_counts(summary: dict[str, object]) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for key in COUNT_KEYS:
        value = summary.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        values[key] = value
    return values


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one explicit after-hours Quant Guardian QMT recovery drill."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=210)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != CONFIRMATION:
        raise SystemExit(f"Refusing live action: --confirm must equal {CONFIRMATION}")

    config_path = args.config.resolve(strict=True)
    config = load_config(config_path)
    if config.mode != "recover":
        raise SystemExit("Refusing live action: isolated drill config is not in recover mode")

    now = datetime.now().astimezone()
    schedule = TradingCalendar(config.trading, config.monitoring).schedule_at(now)
    if schedule.mode != "idle":
        raise SystemExit("Refusing live action: the controlled drill is restricted to idle hours")
    if not config.monitoring.allow_idle_recovery:
        raise SystemExit("Refusing live action: idle recovery is disabled in the drill config")

    gate = SafetyGate(config).status()
    if not gate.live_actions_allowed:
        raise SystemExit(f"Refusing live action: safety gate is closed ({gate.reason})")

    monitor = QmtProcessMonitor(config.qmt)
    before = _identities(monitor)
    before_main = _main_pids(before)
    if not before_main:
        raise SystemExit("Refusing live action: no validated XtMiniQmt process exists")

    report: dict[str, object] = {
        "schema_version": 1,
        "started_at": now.isoformat(),
        "schedule_mode": schedule.mode,
        "calendar_source": schedule.source,
        "safety_gate_open": True,
        "before": {"processes": before},
        "checks": [],
        "success": False,
    }
    _write_report(args.output, report)

    service = GuardianService(config, process_monitor=monitor, now=now)
    try:
        # Establish a real health snapshot and wait for the isolated business
        # session before changing any process state.
        baseline_deadline = time.monotonic() + min(45, args.timeout // 3)
        baseline_status = service.run_once()
        baseline_counts: dict[str, int] | None = None
        while time.monotonic() < baseline_deadline:
            baseline_counts = _business_counts(baseline_status.business_summary)
            qmt_state = baseline_status.components.get("qmt_api", {}).get("state")
            if qmt_state == "healthy" and baseline_counts is not None:
                break
            time.sleep(2)
            baseline_status = service.run_once()
        else:
            raise RuntimeError("baseline QMT API or business summary did not become healthy")

        report["before"] = {
            "processes": before,
            "qmt_api_state": baseline_status.components["qmt_api"]["state"],
            "probe_status": baseline_status.probe.get("status"),
            "business_status": baseline_status.business_summary.get("status"),
            "business_counts": baseline_counts,
            "business_latency_ms": baseline_status.business_summary.get("latency_ms"),
        }
        _write_report(args.output, report)
        print("baseline_verified", flush=True)

        recovery_status = service.manual_restart(operator_confirmed=True)
        if recovery_status.state.value not in {"verifying", "recovering"}:
            raise RuntimeError(
                f"recovery did not enter verification: {recovery_status.state.value}"
            )
        report["recovery_requested_at"] = datetime.now().astimezone().isoformat()
        report["business_invalidated_after_launch"] = (
            recovery_status.business_summary.get("status") == "pending"
        )
        _write_report(args.output, report)
        print("restart_launched", flush=True)

        deadline = time.monotonic() + args.timeout
        final_status = recovery_status
        final_processes: list[dict[str, object]] = []
        final_counts: dict[str, int] | None = None
        while time.monotonic() < deadline:
            time.sleep(5)
            final_status = service.run_once()
            final_processes = _identities(monitor)
            final_main = _main_pids(final_processes)
            final_counts = _business_counts(final_status.business_summary)
            check = {
                "at": datetime.now().astimezone().isoformat(),
                "guardian_state": final_status.state.value,
                "qmt_api_state": final_status.components.get("qmt_api", {}).get("state"),
                "probe_status": final_status.probe.get("status"),
                "business_status": final_status.business_summary.get("status"),
                "business_counts_match_baseline": final_counts == baseline_counts,
                "new_main_pid_seen": bool(final_main and final_main.isdisjoint(before_main)),
            }
            checks = report["checks"]
            assert isinstance(checks, list)
            checks.append(check)
            report["checks"] = checks[-12:]
            _write_report(args.output, report)
            print(json.dumps(check, ensure_ascii=False), flush=True)

            if (
                final_status.state.value == "healthy"
                and final_status.components.get("qmt_api", {}).get("state") == "healthy"
                and final_status.probe.get("status") == "healthy"
                and final_counts is not None
                and final_counts == baseline_counts
                and final_main
                and final_main.isdisjoint(before_main)
            ):
                break
        else:
            raise RuntimeError(
                "QMT recovery did not restore stable business counts before timeout"
            )

        report["success"] = True
        report["completed_at"] = datetime.now().astimezone().isoformat()
        report["after"] = {
            "processes": final_processes,
            "guardian_state": final_status.state.value,
            "qmt_api_state": final_status.components["qmt_api"]["state"],
            "probe_status": final_status.probe.get("status"),
            "probe_latency_ms": final_status.probe.get("latency_ms"),
            "business_status": final_status.business_summary.get("status"),
            "business_counts": final_counts,
            "business_latency_ms": final_status.business_summary.get("latency_ms"),
        }
        _write_report(args.output, report)
        print(f"controlled_recovery_passed report={args.output}", flush=True)
        return 0
    except Exception as exc:
        report["failed_at"] = datetime.now().astimezone().isoformat()
        report["failure"] = f"{type(exc).__name__}: {exc}"
        _write_report(args.output, report)
        raise
    finally:
        service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
