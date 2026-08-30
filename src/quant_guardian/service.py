from __future__ import annotations

import json
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from quant_guardian.config import AppConfig, ensure_runtime_directories
from quant_guardian.diagnostics.audit import AuditLogger
from quant_guardian.diagnostics.exporter import DiagnosticExporter
from quant_guardian.diagnostics.store import MonitoringStore
from quant_guardian.domain.components import ComponentNode, ComponentState
from quant_guardian.domain.models import (
    GuardianState,
    HealthSnapshot,
    LogSignal,
    ProbeStatus,
    ProcessStatus,
    RecommendedAction,
    Transition,
)
from quant_guardian.domain.state_machine import GuardianStateMachine, StateMachinePolicy
from quant_guardian.domain.trading_calendar import ScheduleDecision, TradingCalendar
from quant_guardian.monitors.log_monitor import LogObservation, QmtLogMonitor
from quant_guardian.monitors.network_monitor import NetworkMonitor
from quant_guardian.monitors.process_monitor import ProcessObservation, QmtProcessMonitor
from quant_guardian.monitors.rocket_monitor import RocketMonitor, RocketObservation
from quant_guardian.monitors.trade_system_monitor import (
    TradeSystemMonitor,
    TradeSystemObservation,
)
from quant_guardian.notifications import NotificationCenter
from quant_guardian.probe.business import BusinessProbeManager
from quant_guardian.probe.supervisor import ProbeObservation, ProbeSupervisor
from quant_guardian.reconciliation import decide_reconciliation
from quant_guardian.recovery.controller import RecoveryController
from quant_guardian.recovery.quantclass_controller import QuantclassController
from quant_guardian.safety import SafetyGate


class ProcessMonitorLike(Protocol):
    def observe(self) -> ProcessObservation: ...


class LogMonitorLike(Protocol):
    def observe(self, now: datetime | None = None) -> LogObservation: ...


class NetworkMonitorLike(Protocol):
    def is_available(self) -> bool: ...


class RocketMonitorLike(Protocol):
    def observe(self, now: datetime | None = None) -> RocketObservation: ...


class ProbeLike(Protocol):
    def health(self) -> ProbeObservation: ...
    def reconcile(self) -> ProbeObservation: ...
    def stop(self) -> None: ...


class RecoveryLike(Protocol):
    def recover(self, snapshot: HealthSnapshot, *, event_id: str): ...
    def restart_manually(self, snapshot: HealthSnapshot, *, event_id: str): ...


class QuantclassControllerLike(Protocol):
    def restart(self, *, event_id: str): ...


class TradeSystemMonitorLike(Protocol):
    def observe(
        self,
        now: datetime,
        *,
        rocket: RocketObservation,
        active_window: bool,
    ) -> TradeSystemObservation: ...


class _PassiveBusinessProbe:
    """Used by deterministic tests that inject a fake primary probe."""

    @property
    def latest(self) -> dict[str, object]:
        return {
            "status": "pending",
            "reason": "后台业务汇总未在注入探针模式下启动",
            "latency_ms": 0,
        }

    def maybe_request(self, now: datetime, *, active: bool) -> None:
        return None

    def invalidate_after_recovery(self, now: datetime) -> None:
        return None

    def stop(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    state: GuardianState
    action: RecommendedAction
    reason: str
    observed_at: datetime
    safety_live_actions: bool
    safety_reason: str
    process: dict[str, object] = field(default_factory=dict)
    probe: dict[str, object] = field(default_factory=dict)
    log: dict[str, object] = field(default_factory=dict)
    rocket: dict[str, object] = field(default_factory=dict)
    reconciliation: dict[str, object] = field(default_factory=dict)
    business_summary: dict[str, object] = field(default_factory=dict)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    attention: dict[str, object] = field(default_factory=dict)
    schedule: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["schema_version"] = 2
        value["state"] = self.state.value
        value["action"] = self.action.value
        value["observed_at"] = self.observed_at.isoformat()
        return value


def _component_state_from_process(value: ProcessStatus) -> ComponentState:
    if value is ProcessStatus.HEALTHY:
        return ComponentState.HEALTHY
    if value in {
        ProcessStatus.MISSING,
        ProcessStatus.UNRESPONSIVE,
        ProcessStatus.IDENTITY_MISMATCH,
    }:
        return ComponentState.CRITICAL
    return ComponentState.UNKNOWN


def _component_state_from_probe(value: ProbeStatus) -> ComponentState:
    if value is ProbeStatus.HEALTHY:
        return ComponentState.HEALTHY
    if value in {ProbeStatus.FAILED, ProbeStatus.TIMEOUT}:
        return ComponentState.CRITICAL
    if value in {ProbeStatus.STARTING, ProbeStatus.UNAVAILABLE}:
        return ComponentState.WARNING
    return ComponentState.UNKNOWN


_MARKET_CLOSED_IDLE_ACCOUNT_STATUSES = frozenset(
    {
        "waiting_login",
        "logging_in",
        "login_failed",
        "initializing",
        "correcting",
        "penetration_link_disconnected",
        "system_disabled",
    }
)


class GuardianService:
    def __init__(
        self,
        config: AppConfig,
        *,
        process_monitor: ProcessMonitorLike | None = None,
        log_monitor: LogMonitorLike | None = None,
        network_monitor: NetworkMonitorLike | None = None,
        rocket_monitor: RocketMonitorLike | None = None,
        trade_system_monitor: TradeSystemMonitorLike | None = None,
        probe: ProbeLike | None = None,
        recovery: RecoveryLike | None = None,
        quantclass_controller: QuantclassControllerLike | None = None,
        audit: AuditLogger | None = None,
        safety_gate: SafetyGate | None = None,
        now: datetime | None = None,
    ) -> None:
        self.config = config
        directories = ensure_runtime_directories()
        self.audit = audit or AuditLogger(
            directories["logs"], config.diagnostics.retention_days
        )
        audit_directory = getattr(self.audit, "log_directory", directories["logs"])
        self.store = MonitoringStore(
            directories["root"] / "monitoring.db",
            audit_directory=Path(audit_directory),
            retention_days=config.diagnostics.retention_days,
            enabled=config.diagnostics.sqlite_index_enabled,
        )
        if hasattr(self.audit, "subscribe"):
            self.audit.subscribe(self.store.enqueue_event)
        self.safety_gate = safety_gate or SafetyGate(config)
        self.process_monitor = process_monitor or QmtProcessMonitor(config.qmt)
        self.log_monitor = log_monitor or QmtLogMonitor(
            Path(config.qmt.log_directory), config.thresholds.log_stale_seconds
        )
        self.network_monitor = network_monitor or NetworkMonitor()
        self.rocket_monitor = rocket_monitor or RocketMonitor(config.rocket)
        self.trade_system_monitor = trade_system_monitor or TradeSystemMonitor(
            config.trade_system
        )
        injected_probe = probe is not None
        self.probe = probe or ProbeSupervisor(config.probe, config.qmt)
        self.recovery = recovery or RecoveryController(
            config,
            self.process_monitor,  # type: ignore[arg-type]
            self.safety_gate,
            self.audit,
        )
        self.quantclass_controller = quantclass_controller or QuantclassController(config)
        self.calendar = TradingCalendar(config.trading, config.monitoring)
        self._calendar_cache_path = directories["state"] / "trading-days.json"
        self._load_calendar_cache()
        self.business = (
            _PassiveBusinessProbe()
            if injected_probe
            else BusinessProbeManager(
                config,
                calendar_listener=self._update_calendar_cache,
            )
        )
        self.machine = GuardianStateMachine(
            StateMachinePolicy.from_config(config), now=now
        )
        self.notifications = NotificationCenter(config.notifications.dedupe_minutes)
        self._runtime_root = directories["root"]
        self._poll_lock = threading.Lock()
        self._manual_action_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._listeners: list[Callable[[ServiceStatus], None]] = []
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._monitor_consecutive_errors = 0
        self._last_monitor_success_at: datetime | None = None
        self._expected_monitor_check_at: datetime | None = None
        self._expected_monitor_interval_seconds: float | None = None
        self._monitor_heartbeat_path = directories["state"] / "monitor-heartbeat.json"
        self._last_signature: tuple[object, ...] | None = None
        self._last_trade_signature: tuple[object, ...] | None = None
        self._manual_after_recovery = False
        self._last_reconciliation: dict[str, object] = {}
        self._idle_failure_count = 0
        self._last_idle_failure_at: datetime | None = None
        self._isolated_probe_degraded = False
        self._last_trade_observation: TradeSystemObservation | None = None
        self._last_schedule: ScheduleDecision | None = None
        self._current_incident_id = ""
        self._incident_started_at: datetime | None = None
        self._incident_attempts = 0
        self._active_recovery: dict[str, object] | None = None
        safety = self.safety_gate.status()
        started = now or datetime.now().astimezone()
        initial_schedule = self.calendar.schedule_at(started)
        self._status = ServiceStatus(
            state=self.machine.state,
            action=RecommendedAction.WAIT,
            reason="正在启动监控服务",
            observed_at=started,
            safety_live_actions=safety.live_actions_allowed,
            safety_reason=safety.reason,
            attention={
                "required": False,
                "level": "info",
                "title": "正在建立监控基线",
                "message": "等待首次QMT API与Trade System检查",
                "action": "立即检测",
                "target": "check",
            },
            schedule=self._schedule_dict(
                started, initial_schedule, anomalous=False
            ),
        )

    def _load_calendar_cache(self) -> None:
        try:
            document = json.loads(
                self._calendar_cache_path.read_text(encoding="utf-8-sig")
            )
            dates = document.get("trading_dates")
            coverage = date.fromisoformat(str(document.get("coverage_end")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if isinstance(dates, list):
            parsed_dates: list[date] = []
            for value in dates:
                try:
                    parsed_dates.append(date.fromisoformat(str(value)))
                except ValueError:
                    continue
            if parsed_dates:
                coverage = min(coverage, max(parsed_dates))
            self.calendar.update_market_dates(
                [str(value) for value in dates], coverage_end=coverage
            )

    def _update_calendar_cache(self, values: list[str], coverage: date) -> None:
        self.calendar.update_market_dates(values, coverage_end=coverage)
        document = {
            "schema_version": 1,
            "market": self.config.trading.market,
            "coverage_end": coverage.isoformat(),
            "trading_dates": sorted(set(values)),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        try:
            temporary = self._calendar_cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._calendar_cache_path)
        except OSError:
            return
        self.request_check()

    @property
    def status(self) -> ServiceStatus:
        with self._status_lock:
            return self._status

    def subscribe(self, listener: Callable[[ServiceStatus], None]) -> None:
        self._listeners.append(listener)

    def _publish_status(self, status: ServiceStatus) -> None:
        with self._status_lock:
            self._status = status
        self.store.enqueue_sample(status.to_dict())
        for listener in tuple(self._listeners):
            try:
                listener(status)
            except Exception as exc:
                self.audit.record(
                    "status_listener_error",
                    {
                        "component_id": "quant_guardian",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    severity="warning",
                )

    @staticmethod
    def _new_identifier(prefix: str, at: datetime) -> str:
        return f"{prefix}-{at:%Y%m%d}-{uuid.uuid4().hex[:8]}"

    def _ensure_incident(
        self,
        at: datetime,
        *,
        reason: str,
        snapshot: HealthSnapshot | None,
    ) -> str:
        if self._current_incident_id:
            return self._current_incident_id
        incident_id = self._new_identifier("QGI", at)
        self._current_incident_id = incident_id
        self._incident_started_at = at
        self._incident_attempts = 0
        self.audit.record(
            "incident_started",
            {
                "component_id": "qmt_api",
                "incident_id": incident_id,
                "context": "production",
                "status": "open",
                "result": "in_progress",
                "reason": reason,
                "snapshot": snapshot,
            },
            severity="warning",
            moment=at,
            event_id=incident_id,
        )
        return incident_id

    def _begin_recovery_operation(
        self,
        at: datetime,
        *,
        manual: bool,
        snapshot: HealthSnapshot,
        initiator: str = "",
        remote_channel: str = "",
        remote_request_id: str = "",
    ) -> dict[str, object]:
        incident_id = self._current_incident_id
        if not manual and not incident_id:
            incident_id = self._ensure_incident(
                at,
                reason="automatic QMT recovery started after confirmed failure",
                snapshot=snapshot,
            )
        if incident_id:
            self._incident_attempts += 1
            attempt_no = self._incident_attempts
        else:
            attempt_no = 1
        operation = {
            "operation_id": self._new_identifier("QGO", at),
            "incident_id": incident_id,
            "operation_type": "qmt_restart",
            "initiator": initiator or ("manual" if manual else "automatic"),
            "target_component": "qmt_api",
            "context": "production",
            "attempt_no": attempt_no,
            "started_at": at,
            "manual": manual,
        }
        if remote_channel:
            operation["remote_channel"] = remote_channel
        if remote_request_id:
            operation["remote_request_id"] = remote_request_id
        self._active_recovery = operation
        return operation

    def _finish_active_recovery(
        self,
        at: datetime,
        *,
        success: bool,
        reason: str,
        snapshot: HealthSnapshot | None,
        stage: str,
    ) -> None:
        operation = self._active_recovery
        if not operation:
            return
        started_at = operation.get("started_at")
        duration_ms = (
            max(0, int((at - started_at).total_seconds() * 1000))
            if isinstance(started_at, datetime)
            else 0
        )
        manual = bool(operation.get("manual"))
        self.audit.record(
            (
                "manual_qmt_restart_verified"
                if manual and success
                else "manual_qmt_restart_verification_failed"
                if manual
                else "recovery_verified"
                if success
                else "recovery_verification_failed"
            ),
            {
                **operation,
                "component_id": "qmt_api",
                "phase": stage,
                "status": "succeeded" if success else "failed",
                "result": "succeeded" if success else "failed",
                "success": success,
                "completed_at": at,
                "duration_ms": duration_ms,
                "reason": reason,
                "snapshot": snapshot,
            },
            severity="info" if success else "critical",
            moment=at,
            event_id=str(operation["operation_id"]),
        )
        self._active_recovery = None

    def _resolve_current_incident(
        self,
        at: datetime,
        *,
        reason: str,
        snapshot: HealthSnapshot | None,
    ) -> None:
        incident_id = self._current_incident_id
        if not incident_id:
            return
        duration_ms = (
            max(0, int((at - self._incident_started_at).total_seconds() * 1000))
            if self._incident_started_at
            else 0
        )
        self.audit.record(
            "incident_resolved",
            {
                "component_id": "qmt_api",
                "incident_id": incident_id,
                "context": "production",
                "status": "resolved",
                "result": "succeeded",
                "success": True,
                "attempt_count": self._incident_attempts,
                "started_at": self._incident_started_at,
                "resolved_at": at,
                "duration_ms": duration_ms,
                "reason": reason,
                "snapshot": snapshot,
            },
            severity="info",
            moment=at,
            event_id=incident_id,
        )
        self._current_incident_id = ""
        self._incident_started_at = None
        self._incident_attempts = 0

    def _record_transition(self, transition: Transition) -> None:
        if (
            transition.snapshot is not None
            and not transition.snapshot.is_healthy
            and transition.new_state
            in {GuardianState.SUSPECT, GuardianState.DEGRADED}
        ):
            self._ensure_incident(
                transition.at,
                reason=transition.reason,
                snapshot=transition.snapshot,
            )
        signature = (
            transition.old_state,
            transition.new_state,
            transition.action,
            transition.reason,
        )
        if signature != self._last_signature:
            self.audit.record_transition(
                transition,
                incident_id=self._current_incident_id,
                operation_id=(
                    str(self._active_recovery.get("operation_id") or "")
                    if self._active_recovery
                    else ""
                ),
            )
            self._last_signature = signature
        verification_failed = (
            "recovery verification failed" in transition.reason
            or "did not pass stable verification" in transition.reason
        )
        if verification_failed and self._active_recovery:
            self._finish_active_recovery(
                transition.at,
                success=False,
                reason=transition.reason,
                snapshot=transition.snapshot,
                stage="verification",
            )
        if (
            transition.new_state is GuardianState.HEALTHY
            and transition.reason == "stable health verification completed"
        ):
            if self._active_recovery:
                self._finish_active_recovery(
                    transition.at,
                    success=True,
                    reason=transition.reason,
                    snapshot=transition.snapshot,
                    stage="verification",
                )
            self._resolve_current_incident(
                transition.at,
                reason=transition.reason,
                snapshot=transition.snapshot,
            )
        if transition.new_state is GuardianState.MANUAL_REQUIRED:
            self.notifications.publish(
                "Quant Guardian需要人工处理",
                transition.reason,
                severity="critical",
                event_key="manual_required",
                now=transition.at,
            )
        elif transition.new_state is GuardianState.LOCKOUT:
            self.notifications.publish(
                "Quant Guardian已锁定自动恢复",
                transition.reason,
                severity="critical",
                event_key="lockout",
                now=transition.at,
            )
        elif transition.new_state is GuardianState.HEALTHY:
            self.notifications.publish(
                "QMT API链路健康",
                transition.reason,
                severity="info",
                event_key="healthy",
                now=transition.at,
            )

    def _record_trade_system(self, observation: TradeSystemObservation) -> None:
        children = observation.node.children
        flattened = [*children]
        for child in children:
            flattened.extend(child.children)
        actionable = [
            child
            for child in flattened
            if child.state in {ComponentState.CRITICAL, ComponentState.WARNING}
            and child.metrics.get("selected") is not False
        ]
        signature = tuple(
            sorted(
                (
                    child.id,
                    child.state.value,
                    str(child.metrics.get("condition") or child.reason),
                )
                for child in actionable
            )
        )
        previous_signature = self._last_trade_signature
        if signature == previous_signature:
            return
        self._last_trade_signature = signature
        severity = (
            "critical"
            if any(child.state is ComponentState.CRITICAL for child in actionable)
            else "warning"
            if actionable
            else "info"
        )
        culprit = max(
            actionable,
            key=lambda child: (
                2 if child.state is ComponentState.CRITICAL else 1,
                1 if child.priority == "high" else 0,
                1 if not child.children else 0,
            ),
            default=observation.node,
        )
        engine = str(culprit.metrics.get("engine") or culprit.name)
        summary = (
            f"{engine}：{culprit.reason}"
            if culprit is not observation.node
            else observation.node.reason
        )
        self.audit.record(
            "trade_system_state",
            {
                "component_id": "trade_system",
                "subcomponent_id": culprit.id,
                "state": observation.node.state.value,
                "reason": observation.node.reason,
                "summary": summary,
                "components": [child.to_dict() for child in children],
            },
            severity=severity,
            moment=observation.node.observed_at,
        )
        if severity in {"critical", "warning"}:
            issue_code = str(culprit.metrics.get("condition") or culprit.id)
            self.notifications.publish(
                "Trade System需要处理",
                summary,
                severity=severity,
                event_key=(
                    f"trade_system:{culprit.id}:{culprit.state.value}:{issue_code}"
                ),
                now=observation.node.observed_at,
            )
        elif previous_signature:
            self.notifications.publish(
                "Trade System已恢复",
                observation.node.reason,
                severity="info",
                event_key="trade_system:recovered",
                now=observation.node.observed_at,
            )

    @staticmethod
    def _process_details(observation: ProcessObservation) -> dict[str, object]:
        return {
            "status": observation.status.value,
            "reason": observation.reason,
            "processes": [
                {
                    "pid": process.pid,
                    "name": process.name,
                    "responsive": process.responsive,
                }
                for process in observation.processes
            ],
        }

    @staticmethod
    def _probe_details(observation: ProbeObservation) -> dict[str, object]:
        return {
            "status": observation.status.value,
            "reason": observation.reason,
            "latency_ms": observation.latency_ms,
            "account_status": observation.account_status,
            "account_ref": observation.account_ref,
            "details": dict(observation.details or {}),
        }

    @staticmethod
    def _log_details(observation: LogObservation) -> dict[str, object]:
        return {
            "signal": observation.signal.value,
            "reason": observation.reason,
            "last_modified": (
                observation.last_modified.isoformat()
                if observation.last_modified
                else ""
            ),
            "login_requires_manual": observation.login_requires_manual,
        }

    @staticmethod
    def _rocket_details(observation: RocketObservation) -> dict[str, object]:
        return {
            "active": observation.active,
            "error_burst": observation.error_burst,
            "reason": observation.reason,
            "log_age_seconds": observation.log_age_seconds,
            "business_healthy": observation.business_healthy,
            "business_age_seconds": observation.business_age_seconds,
            "heartbeat_source": observation.heartbeat_source,
            "business_health_known": observation.business_health_known,
        }

    @staticmethod
    def _is_market_closed_session_idle(
        schedule: ScheduleDecision,
        snapshot: HealthSnapshot,
        probe: ProbeObservation,
    ) -> bool:
        """Recognize a closed-market broker session without masking QMT failures."""

        closed_session_failure = (
            probe.status is ProbeStatus.TIMEOUT
            or (
                probe.status is ProbeStatus.FAILED
                and probe.reason == "QMT account login status is not healthy"
                and probe.account_status in _MARKET_CLOSED_IDLE_ACCOUNT_STATUSES
            )
        )
        return (
            not schedule.trading_day
            and snapshot.process_status is ProcessStatus.HEALTHY
            and snapshot.network_available
            and not snapshot.login_requires_manual
            and closed_session_failure
        )

    def _idle_confirmation_required(self) -> int:
        return max(
            self.config.monitoring.anomaly_confirmation_checks,
            self.config.thresholds.failure_threshold,
        )

    def _rocket_expected_active(
        self,
        at: datetime,
        schedule: ScheduleDecision,
    ) -> bool:
        if not schedule.trading_day:
            return False
        start_hour, start_minute = (
            int(value)
            for value in self.config.trade_system.rocket_expected_start.split(":", 1)
        )
        end_hour, end_minute = (
            int(value) for value in self.config.trading.postmarket_end.split(":", 1)
        )
        expected_at = at.replace(
            hour=start_hour,
            minute=start_minute,
            second=0,
            microsecond=0,
        ) + timedelta(seconds=self.config.trade_system.rocket_startup_grace_seconds)
        end_at = at.replace(
            hour=end_hour,
            minute=end_minute,
            second=0,
            microsecond=0,
        )
        return expected_at <= at <= end_at

    @staticmethod
    def _is_isolated_probe_degraded(
        process: ProcessObservation,
        probe: ProbeObservation,
        log: LogObservation,
        network_available: bool,
        rocket: RocketObservation,
    ) -> bool:
        corroborating_business = (
            rocket.business_healthy or log.signal is LogSignal.POSITIVE
        )
        contradictory_log = log.signal in {
            LogSignal.EXPLICIT_DISCONNECT,
            LogSignal.LOGIN_FAILURE,
        }
        return (
            process.status is ProcessStatus.HEALTHY
            and probe.status is ProbeStatus.TIMEOUT
            and network_available
            and not log.login_requires_manual
            and corroborating_business
            and not contradictory_log
        )

    def _record_probe_correlation(
        self,
        at: datetime,
        degraded: bool,
        probe: ProbeObservation,
        log: LogObservation,
        rocket: RocketObservation,
    ) -> None:
        if degraded == self._isolated_probe_degraded:
            return
        self._isolated_probe_degraded = degraded
        self.audit.record(
            "qmt_probe_degraded" if degraded else "qmt_probe_correlation_restored",
            {
                "component_id": "qmt_api.xtquant",
                "raw_probe_status": probe.status.value,
                "probe_reason": probe.reason,
                "qmt_log_signal": log.signal.value,
                "rocket_process_active": rocket.active,
                "rocket_business_healthy": rocket.business_healthy,
                "rocket_business_health_known": rocket.business_health_known,
                "rocket_business_age_seconds": rocket.business_age_seconds,
                "automatic_recovery_suppressed": degraded,
                "reason": (
                    "independent Guardian probe timed out while a separate "
                    "business signal remained healthy"
                    if degraded
                    else "independent probe correlation returned to normal"
                ),
            },
            severity="warning" if degraded else "info",
            moment=at,
        )

    def _qmt_component(
        self,
        at: datetime,
        transition: Transition,
        process: ProcessObservation,
        probe: ProbeObservation,
        log: LogObservation,
        business: dict[str, object],
        schedule: ScheduleDecision,
        *,
        market_closed_session_idle: bool,
        isolated_probe_degraded: bool = False,
    ) -> ComponentNode:
        process_node = ComponentNode(
            id="qmt_api.process",
            name="QMT进程",
            state=_component_state_from_process(process.status),
            reason=process.reason,
            observed_at=at,
            priority="high",
            metrics=self._process_details(process),
        )
        probe_state = (
            ComponentState.IDLE
            if market_closed_session_idle
            else ComponentState.WARNING
            if isolated_probe_degraded
            else _component_state_from_probe(probe.status)
        )
        connection_node = ComponentNode(
            id="qmt_api.xtquant",
            name="XTQuant连接",
            state=probe_state,
            reason=(
                "休市期间券商交易会话暂不可用，不视为故障"
                if market_closed_session_idle
                else "Guardian独立探针超时；Rocket/QMT业务信号仍正常"
                if isolated_probe_degraded
                else
                f"账户状态：{probe.account_status}"
                if probe.status is ProbeStatus.HEALTHY
                else probe.reason
            ),
            observed_at=at,
            priority="high",
            metrics={
                "latency_ms": probe.latency_ms,
                "account_status": probe.account_status,
            },
        )
        asset_valid = bool((probe.details or {}).get("asset_object_valid"))
        account_node = ComponentNode(
            id="qmt_api.account",
            name="账户只读查询",
            state=probe_state,
            reason=(
                "休市期间账户只读查询暂停，不视为故障"
                if market_closed_session_idle
                else "独立只读查询暂时超时，业务侧仍有成功心跳"
                if isolated_probe_degraded
                else
                "资产对象有效，只读查询正常"
                if probe.status is ProbeStatus.HEALTHY and asset_valid
                else probe.reason
            ),
            observed_at=at,
            priority="high",
            metrics={
                "latency_ms": probe.latency_ms,
                "asset_object_valid": asset_valid,
            },
        )
        business_status = str(business.get("status") or "pending")
        business_state = (
            ComponentState.IDLE
            if market_closed_session_idle
            else ComponentState.HEALTHY
            if business_status == "healthy"
            else ComponentState.WARNING
            if business_status in {"unavailable", "stale"}
            else ComponentState.UNKNOWN
        )
        business_node = ComponentNode(
            id="qmt_api.trading_snapshot",
            name="委托与成交",
            state=business_state,
            reason=(
                "休市期间不刷新委托与成交，保留最近一次只读结果"
                if market_closed_session_idle
                else str(business.get("reason") or "等待只读业务汇总")
            ),
            observed_at=at,
            priority="normal",
            metrics={
                key: value
                for key, value in business.items()
                if key
                in {
                    "orders",
                    "cancelable_orders",
                    "trades",
                    "positions",
                    "latency_ms",
                    "sampled_at",
                    "last_success_at",
                    "last_attempt_at",
                    "next_retry_at",
                    "consecutive_failures",
                    "stale",
                    "status",
                }
            },
        )
        critical_children = (process_node, connection_node, account_node)
        if market_closed_session_idle:
            state = ComponentState.IDLE
        elif any(child.state is ComponentState.CRITICAL for child in critical_children):
            state = ComponentState.CRITICAL
        elif any(child.state is ComponentState.WARNING for child in critical_children):
            state = ComponentState.WARNING
        elif all(child.state is ComponentState.HEALTHY for child in critical_children):
            state = ComponentState.HEALTHY
        else:
            state = ComponentState.UNKNOWN
        if (
            not market_closed_session_idle
            and transition.new_state
            in {GuardianState.RECOVERING, GuardianState.VERIFYING}
        ):
            state = ComponentState.RECOVERING
        reason = (
            "休市日：QMT进程正常，交易连接暂不可用；无需处理"
            if market_closed_session_idle
            else "独立探针暂时超时；业务链路仍正常，不触发自动恢复"
            if isolated_probe_degraded
            else "进程、XTQuant连接和账户只读查询正常"
            if state is ComponentState.HEALTHY
            else transition.reason
        )
        return ComponentNode(
            id="qmt_api",
            name="QMT API",
            state=state,
            reason=reason,
            observed_at=at,
            children=(process_node, connection_node, account_node, business_node),
            metrics={
                "log_signal": log.signal.value,
                "log_reason": log.reason,
                "log_is_supporting_evidence": True,
                "market_closed_session_idle": market_closed_session_idle,
                "isolated_probe_degraded": isolated_probe_degraded,
                "calendar_source": schedule.source,
                "raw_probe_state": probe.status.value,
            },
        )

    @staticmethod
    def _attention(
        transition: Transition,
        qmt: ComponentNode,
        trade: ComponentNode,
    ) -> dict[str, object]:
        if transition.new_state is GuardianState.MANUAL_REQUIRED:
            return {
                "required": True,
                "level": "critical",
                "title": "需要人工核对实盘状态",
                "message": transition.reason,
                "action": "核对并确认",
                "target": "manual",
            }
        if transition.new_state is GuardianState.LOCKOUT:
            return {
                "required": True,
                "level": "critical",
                "title": "自动恢复已锁定",
                "message": transition.reason,
                "action": "检查后解除锁定",
                "target": "unlock",
            }
        if bool(qmt.metrics.get("isolated_probe_degraded")):
            return {
                "required": False,
                "level": "info",
                "title": "QMT业务链路仍在运行",
                "message": qmt.reason,
                "action": "查看监控证据",
                "target": "monitor",
            }
        if qmt.state in {ComponentState.CRITICAL, ComponentState.WARNING}:
            return {
                "required": True,
                "level": "critical" if qmt.state is ComponentState.CRITICAL else "warning",
                "title": "QMT API需要处理",
                "message": qmt.reason,
                "action": (
                    "受控重启QMT"
                    if transition.action is RecommendedAction.RECOVER_QMT
                    else "查看监控证据"
                ),
                "target": (
                    "restart"
                    if transition.action is RecommendedAction.RECOVER_QMT
                    else "monitor"
                ),
            }
        if trade.state in {ComponentState.CRITICAL, ComponentState.WARNING}:
            return {
                "required": True,
                "level": "critical" if trade.state is ComponentState.CRITICAL else "warning",
                "title": "Trade System需要处理",
                "message": trade.reason,
                "action": "查看Trade System事件",
                "target": "monitor",
            }
        if qmt.state is ComponentState.IDLE:
            return {
                "required": False,
                "level": "neutral",
                "title": "今日休市，无需操作",
                "message": qmt.reason,
                "action": "立即检测",
                "target": "check",
            }
        return {
            "required": False,
            "level": "success",
            "title": "当前无需操作",
            "message": "QMT API与关键交易内核没有需要你处理的异常",
            "action": "立即检测",
            "target": "check",
        }

    def _schedule_dict(
        self,
        at: datetime,
        decision: ScheduleDecision,
        *,
        anomalous: bool,
        verifying: bool = False,
    ) -> dict[str, object]:
        next_check_at = self.calendar.next_check_at(
            at,
            anomalous=(anomalous or verifying),
        )
        interval = max(0.1, (next_check_at - at).total_seconds())
        return {
            "mode": decision.mode,
            "interval_seconds": interval,
            "nominal_interval_seconds": decision.interval_seconds,
            "trading_day": decision.trading_day,
            "calendar_source": decision.source,
            "calendar_uncertain": decision.uncertain,
            "active_window": (
                f"{self.config.monitoring.active_start}–"
                f"{self.config.monitoring.active_end}"
            ),
            "next_check_at": next_check_at.isoformat(),
            "anomaly_confirmation": {
                "active": anomalous and decision.mode == "idle",
                "current": self._idle_failure_count,
                "required": self._idle_confirmation_required(),
            },
            "burst_reason": (
                "startup_verification"
                if verifying and decision.mode == "idle"
                else "anomaly_confirmation"
                if anomalous and decision.mode == "idle"
                else ""
            ),
        }

    def _build_status(
        self,
        transition: Transition,
        process: ProcessObservation,
        probe: ProbeObservation,
        log: LogObservation,
        rocket: RocketObservation,
        trade: TradeSystemObservation,
        schedule: ScheduleDecision,
        *,
        anomalous_idle: bool,
        market_closed_session_idle: bool = False,
        isolated_probe_degraded: bool = False,
    ) -> ServiceStatus:
        safety = self.safety_gate.status()
        business = self.business.latest
        qmt_node = self._qmt_component(
            transition.at,
            transition,
            process,
            probe,
            log,
            business,
            schedule,
            market_closed_session_idle=market_closed_session_idle,
            isolated_probe_degraded=isolated_probe_degraded,
        )
        trade_node = trade.node
        display_reason = (
            "休市日，QMT进程正常；券商交易会话暂不可用，不触发自动恢复"
            if market_closed_session_idle
            else "独立探针暂时超时；已有业务成功信号，不触发QMT重启"
            if isolated_probe_degraded
            else transition.reason
        )
        return ServiceStatus(
            state=transition.new_state,
            action=transition.action,
            reason=display_reason,
            observed_at=transition.at,
            safety_live_actions=safety.live_actions_allowed,
            safety_reason=safety.reason,
            process=self._process_details(process),
            probe=self._probe_details(probe),
            log=self._log_details(log),
            rocket=self._rocket_details(rocket),
            reconciliation=dict(self._last_reconciliation),
            business_summary=business,
            components={
                "qmt_api": qmt_node.to_dict(),
                "trade_system": trade_node.to_dict(),
            },
            attention=self._attention(transition, qmt_node, trade_node),
            schedule=self._schedule_dict(
                transition.at,
                schedule,
                anomalous=anomalous_idle,
                verifying=(
                    transition.new_state
                    in {
                        GuardianState.STARTING,
                        GuardianState.RECOVERING,
                        GuardianState.VERIFYING,
                    }
                ),
            ),
        )

    def run_once(
        self,
        now: datetime | None = None,
        *,
        wait_for_lock: bool = False,
    ) -> ServiceStatus:
        lock_timeout = max(10.0, float(self.config.probe.timeout_seconds) + 5.0)
        acquired = (
            self._poll_lock.acquire(timeout=lock_timeout)
            if wait_for_lock
            else self._poll_lock.acquire(blocking=False)
        )
        if not acquired:
            if wait_for_lock:
                raise TimeoutError(
                    "monitoring check is still busy after "
                    f"{lock_timeout:g} seconds"
                )
            return self.status
        try:
            at = now or datetime.now().astimezone()
            schedule = self.calendar.schedule_at(at)
            self._last_schedule = schedule
            self.business.maybe_request(at, active=schedule.mode == "active")
            process = self.process_monitor.observe()
            network_available = self.network_monitor.is_available()
            log = self.log_monitor.observe(at)
            rocket = self.rocket_monitor.observe(at)
            trading_phase = self.calendar.phase_at(at)
            trade = self.trade_system_monitor.observe(
                at,
                rocket=rocket,
                # Rocket normally starts near 09:00. The 08:30 Guardian window
                # must not flag the order kernel before its own startup grace.
                active_window=self._rocket_expected_active(at, schedule),
            )
            self._last_trade_observation = trade
            self._record_trade_system(trade)
            if process.status in {
                ProcessStatus.MISSING,
                ProcessStatus.IDENTITY_MISMATCH,
            }:
                probe = ProbeObservation(
                    ProbeStatus.FAILED,
                    "business probe skipped because the validated QMT process is absent",
                )
            else:
                probe = self.probe.health()
            # Runtime logs are supporting evidence. They can explain a failure,
            # but a log-only disconnect cannot make a healthy API restart QMT.
            decision_log = (
                log.signal
                if probe.status is not ProbeStatus.HEALTHY
                else LogSignal.POSITIVE
            )
            snapshot = HealthSnapshot(
                observed_at=at,
                process_status=process.status,
                probe_status=probe.status,
                log_signal=decision_log,
                network_available=network_available,
                rocket_active=rocket.active,
                rocket_business_healthy=(
                    rocket.business_healthy
                    if rocket.business_health_known
                    else None
                ),
                trading_phase=trading_phase,
                account_status=probe.account_status,
                login_requires_manual=log.login_requires_manual,
                details={
                    "process_reason": process.reason,
                    "probe_reason": probe.reason,
                    "log_reason": log.reason,
                    "rocket_error_burst": rocket.error_burst,
                    "rocket_business_healthy": rocket.business_healthy,
                    "rocket_business_health_known": rocket.business_health_known,
                    "rocket_business_age_seconds": rocket.business_age_seconds,
                    "trade_system_state": trade.node.state.value,
                },
            )
            market_closed_session_idle = self._is_market_closed_session_idle(
                schedule,
                snapshot,
                probe,
            )
            isolated_probe_degraded = (
                not market_closed_session_idle
                and self._is_isolated_probe_degraded(
                    process,
                    probe,
                    log,
                    network_available,
                    rocket,
                )
            )
            self._record_probe_correlation(
                at,
                isolated_probe_degraded,
                probe,
                log,
                rocket,
            )
            decision_snapshot = snapshot
            if market_closed_session_idle:
                decision_snapshot = replace(
                    snapshot,
                    probe_status=ProbeStatus.HEALTHY,
                    log_signal=LogSignal.POSITIVE,
                    account_status="market_closed",
                    details={
                        **snapshot.details,
                        "market_closed_session_idle": True,
                        "raw_probe_status": probe.status.value,
                        "raw_account_status": probe.account_status,
                    },
                )
            elif isolated_probe_degraded:
                decision_snapshot = replace(
                    snapshot,
                    probe_status=ProbeStatus.HEALTHY,
                    log_signal=LogSignal.POSITIVE,
                    details={
                        **snapshot.details,
                        "isolated_probe_degraded": True,
                        "raw_probe_status": probe.status.value,
                        "raw_probe_reason": probe.reason,
                    },
                )
            qmt_fault = (
                not decision_snapshot.is_healthy
                and decision_snapshot.network_available
                and not decision_snapshot.login_requires_manual
            )
            decision_grace_active = (
                self.machine.state
                in {GuardianState.STARTING, GuardianState.VERIFYING}
                and at < self.machine.grace_until
            )
            if schedule.mode == "idle" and qmt_fault:
                # A failure inside the state machine's startup/resume grace is
                # evidence for keeping the 15-second burst alive, but it must
                # not consume the independent idle confirmation counter.  If
                # it did, the counter could reach 3/3 during grace and return
                # to hourly polling just as the state machine records 1/3.
                if decision_grace_active:
                    self._idle_failure_count = 0
                    self._last_idle_failure_at = None
                else:
                    if (
                        self._last_idle_failure_at is None
                        or (at - self._last_idle_failure_at).total_seconds()
                        > self.config.thresholds.failure_window_seconds
                    ):
                        self._idle_failure_count = 0
                    self._idle_failure_count = min(
                        self._idle_failure_count + 1,
                        self._idle_confirmation_required(),
                    )
                    self._last_idle_failure_at = at
            else:
                self._idle_failure_count = 0
                self._last_idle_failure_at = None
            idle_confirmed = (
                self._idle_failure_count
                >= self._idle_confirmation_required()
            )
            safety = self.safety_gate.status()
            schedule_permits_recovery = (
                schedule.mode == "active"
                or (
                    schedule.trading_day
                    and self.config.monitoring.allow_idle_recovery
                    and idle_confirmed
                )
            )
            rocket_blocks_automatic_qmt_recovery = (
                snapshot.rocket_blocks_automatic_recovery
                and not self.config.recovery.allow_qmt_restart_while_rocket_active
            )
            recovery_otherwise_permitted = (
                safety.live_actions_allowed and schedule_permits_recovery
            )
            transition = self.machine.observe(
                decision_snapshot,
                recovery_permitted=(
                    recovery_otherwise_permitted
                    and not rocket_blocks_automatic_qmt_recovery
                ),
                recovery_block_reason=(
                    "QMT fault confirmed while Rocket is active with a fresh "
                    "business heartbeat; automatic QMT recovery is suppressed "
                    "and operator intervention is required"
                    if recovery_otherwise_permitted
                    and rocket_blocks_automatic_qmt_recovery
                    else None
                ),
            )
            self._record_transition(transition)

            if (
                transition.new_state is GuardianState.HEALTHY
                and self._manual_after_recovery
            ):
                reconciliation = self.probe.reconcile()
                decision = decide_reconciliation(
                    reconciliation,
                    rocket_active=snapshot.rocket_active,
                    trading_phase=snapshot.trading_phase,
                    require_manual_resume=(
                        self.config.recovery.require_manual_rocket_resume
                    ),
                )
                self._last_reconciliation = {
                    "requires_manual": decision.requires_manual,
                    "reason": decision.reason,
                    **decision.details,
                }
                self.audit.record(
                    "read_only_reconciliation",
                    {
                        "component_id": "qmt_api.trading_snapshot",
                        **self._last_reconciliation,
                    },
                    severity="warning" if decision.requires_manual else "info",
                    moment=at,
                )
                if decision.requires_manual:
                    transition = self.machine.mark_manual_required(at, decision.reason)
                    self._record_transition(transition)
                else:
                    self._manual_after_recovery = False

            idle_recovery_backoff = (
                transition.new_state is GuardianState.DEGRADED
                and transition.action is RecommendedAction.WAIT
                and self.machine.next_attempt_at is not None
                and at < self.machine.next_attempt_at
                and schedule_permits_recovery
            )
            anomalous_idle = (
                schedule.mode == "idle"
                and qmt_fault
                and (
                    not idle_confirmed
                    or transition.new_state
                    in {GuardianState.STARTING, GuardianState.SUSPECT}
                    or idle_recovery_backoff
                )
            )
            status = self._build_status(
                transition,
                process,
                probe,
                log,
                rocket,
                trade,
                schedule,
                anomalous_idle=anomalous_idle,
                market_closed_session_idle=market_closed_session_idle,
                isolated_probe_degraded=isolated_probe_degraded,
            )
            self._publish_status(status)

            if transition.action is RecommendedAction.RECOVER_QMT:
                status = self._execute_recovery(
                    snapshot,
                    process,
                    probe,
                    log,
                    rocket,
                    trade,
                    schedule,
                )
            return status
        finally:
            self._poll_lock.release()

    def _execute_recovery(
        self,
        snapshot: HealthSnapshot,
        process: ProcessObservation,
        probe: ProbeObservation,
        log: LogObservation,
        rocket: RocketObservation,
        trade: TradeSystemObservation,
        schedule: ScheduleDecision,
        *,
        manual: bool = False,
        initiator: str = "",
        remote_channel: str = "",
        remote_request_id: str = "",
    ) -> ServiceStatus:
        started_at = datetime.now().astimezone()
        # The completed confirmation belongs to the recovery attempt that is
        # starting now.  A failed launch must build a fresh evidence window
        # before a later retry, rather than inheriting a stale 3/3 display.
        self._idle_failure_count = 0
        self._last_idle_failure_at = None
        operation = self._begin_recovery_operation(
            started_at,
            manual=manual,
            snapshot=snapshot,
            initiator=initiator,
            remote_channel=remote_channel,
            remote_request_id=remote_request_id,
        )
        event_id = str(operation["operation_id"])
        transition = (
            self.machine.mark_manual_restart_started(started_at)
            if manual
            else self.machine.mark_recovery_started(started_at)
        )
        self._record_transition(transition)
        self.audit.record(
            "manual_qmt_restart_requested" if manual else "recovery_requested",
            {
                **operation,
                "component_id": "qmt_api",
                "phase": "requested",
                "status": "in_progress",
                "result": "in_progress",
                "operator_confirmed": manual,
                "automatic_recovery_gate_bypassed": manual,
                "rocket_active": snapshot.rocket_active,
                "rocket_business_healthy": snapshot.rocket_business_healthy,
                "trading_phase": snapshot.trading_phase,
                "reason": (
                    "remote operator confirmed QMT restart"
                    if remote_channel
                    else "operator confirmed QMT restart"
                    if manual
                    else self.machine.last_transition.reason
                ),
            },
            severity="warning",
            moment=started_at,
            event_id=event_id,
        )
        self._manual_after_recovery = (
            snapshot.rocket_active
            and self.config.recovery.require_manual_rocket_resume
        )
        result = (
            self.recovery.restart_manually(snapshot, event_id=event_id)
            if manual
            else self.recovery.recover(snapshot, event_id=event_id)
        )
        completed_at = datetime.now().astimezone()
        self.audit.record(
            "manual_qmt_restart_result" if manual else "recovery_result",
            {
                **operation,
                "component_id": "qmt_api",
                "phase": "launch",
                "status": "verifying" if result.success else "failed",
                "result": "launch_succeeded" if result.success else "failed",
                "final": not result.success,
                "operator_confirmed": manual,
                "success": result.success,
                "launched": result.launched,
                "live_action": result.live_action,
                "reason": result.reason,
                "details": result.details,
            },
            severity="info" if result.success else "critical",
            moment=completed_at,
            event_id=event_id,
        )
        if not result.success:
            self._finish_active_recovery(
                completed_at,
                success=False,
                reason=result.reason,
                snapshot=snapshot,
                stage="launch",
            )
        transition = (
            self.machine.mark_launch_succeeded(completed_at, manual=manual)
            if result.success
            else (
                self.machine.mark_manual_restart_failed(completed_at, result.reason)
                if manual
                else self.machine.mark_recovery_failed(completed_at, result.reason)
            )
        )
        if result.success:
            reset_probe = getattr(self.probe, "reset_after_recovery", None)
            if callable(reset_probe):
                reset_probe()
            self.business.invalidate_after_recovery(completed_at)
        self._record_transition(transition)
        status = self._build_status(
            transition,
            process,
            probe,
            log,
            rocket,
            trade,
            schedule,
            anomalous_idle=schedule.mode == "idle",
        )
        self._publish_status(status)
        self.notifications.publish(
            (
                "QMT远程重启已启动"
                if remote_channel and result.success
                else "QMT远程重启失败"
                if remote_channel
                else "QMT人工重启已启动"
                if manual and result.success
                else "QMT人工重启失败" if manual
                else "QMT恢复已启动"
                if result.success
                else "QMT恢复失败"
            ),
            result.reason,
            severity="info" if result.success else "critical",
            event_key=(
                "manual_qmt_restart_result" if manual else "recovery_result"
            ),
            now=completed_at,
        )
        return status

    def _record_blocked_qmt_restart(
        self,
        reason: str,
        *,
        phase: str,
        initiator: str = "manual",
        remote_channel: str = "",
        remote_request_id: str = "",
    ) -> None:
        at = datetime.now().astimezone()
        operation_id = self._new_identifier("QGO", at)
        self.audit.record(
            "manual_qmt_restart_rejected",
            {
                "component_id": "qmt_api",
                "operation_id": operation_id,
                "operation_type": "qmt_restart",
                "initiator": initiator,
                "target_component": "qmt_api",
                "context": "production",
                "started_at": at,
                "completed_at": at,
                "status": "blocked",
                "phase": phase,
                "reason": reason,
                "remote_channel": remote_channel,
                "remote_request_id": remote_request_id,
            },
            severity="warning",
            moment=at,
            event_id=operation_id,
        )

    def manual_restart(
        self,
        *,
        operator_confirmed: bool = False,
        initiator: str = "manual",
        remote_channel: str = "",
        remote_request_id: str = "",
    ) -> ServiceStatus:
        allowed_initiators = {"manual", "remote_telegram", "remote_weixin"}
        if initiator not in allowed_initiators:
            raise ValueError("unsupported QMT restart initiator")
        if initiator.startswith("remote_"):
            expected_channel = initiator.removeprefix("remote_")
            if remote_channel != expected_channel or not remote_request_id:
                raise ValueError("remote restart metadata is incomplete")
        if not operator_confirmed:
            self._record_blocked_qmt_restart(
                "explicit operator confirmation was not supplied",
                phase="confirmation",
                initiator=initiator,
                remote_channel=remote_channel,
                remote_request_id=remote_request_id,
            )
            raise PermissionError("重启QMT前必须完成确认")
        if self.machine.state in {
            GuardianState.RECOVERING,
            GuardianState.VERIFYING,
        }:
            self._record_blocked_qmt_restart(
                "another QMT restart is still executing or awaiting stable verification",
                phase="exclusive_verification",
                initiator=initiator,
                remote_channel=remote_channel,
                remote_request_id=remote_request_id,
            )
            raise RuntimeError("QMT重启仍在执行或稳定验证中，请等待当前操作完成")
        if self.machine.last_snapshot is None:
            self.run_once()
        snapshot = self.machine.last_snapshot
        if snapshot is None:
            raise RuntimeError("尚未建立QMT健康基线，无法执行人工重启")
        if not self._manual_action_lock.acquire(blocking=False):
            raise RuntimeError("另一个人工操作正在执行，请稍后再试")
        try:
            now = datetime.now().astimezone()
            schedule = self.calendar.schedule_at(now)
            process = self.process_monitor.observe()
            probe = self.probe.health()
            log = self.log_monitor.observe(now)
            rocket = self.rocket_monitor.observe(now)
            network_available = self.network_monitor.is_available()
            trade = self.trade_system_monitor.observe(
                now,
                rocket=rocket,
                active_window=self._rocket_expected_active(now, schedule),
            )
            current_snapshot = replace(
                snapshot,
                observed_at=now,
                process_status=process.status,
                probe_status=probe.status,
                log_signal=log.signal,
                network_available=network_available,
                rocket_active=rocket.active,
                rocket_business_healthy=(
                    rocket.business_healthy
                    if rocket.business_health_known
                    else None
                ),
                trading_phase=self.calendar.phase_at(now),
                account_status=probe.account_status,
                login_requires_manual=log.login_requires_manual,
            )
            if initiator.startswith("remote_"):
                blocked_reason = (
                    "network is unavailable; remote QMT restart is blocked"
                    if not current_snapshot.network_available
                    else "QMT requires manual login; remote restart is blocked"
                    if current_snapshot.login_requires_manual
                    else ""
                )
                if blocked_reason:
                    self._record_blocked_qmt_restart(
                        blocked_reason,
                        phase="remote_preflight",
                        initiator=initiator,
                        remote_channel=remote_channel,
                        remote_request_id=remote_request_id,
                    )
                    raise PermissionError(blocked_reason)
            status = self._execute_recovery(
                current_snapshot,
                process,
                probe,
                log,
                rocket,
                trade,
                schedule,
                manual=True,
                initiator=initiator,
                remote_channel=remote_channel,
                remote_request_id=remote_request_id,
            )
            # A manual restart runs outside the service loop.  Wake the loop
            # immediately so verification is not delayed by the idle-period
            # interval that was selected before the operator action.
            self.request_check()
            return status
        finally:
            self._manual_action_lock.release()

    def manual_restart_trade_system(
        self, *, operator_confirmed: bool = False
    ) -> ServiceStatus:
        if not operator_confirmed:
            at = datetime.now().astimezone()
            operation_id = self._new_identifier("QGO", at)
            self.audit.record(
                "manual_quantclass_restart_rejected",
                {
                    "component_id": "trade_system.client",
                    "operation_id": operation_id,
                    "operation_type": "quantclass_restart",
                    "initiator": "manual",
                    "target_component": "trade_system.client",
                    "context": "production",
                    "started_at": at,
                    "completed_at": at,
                    "status": "blocked",
                    "phase": "confirmation",
                    "reason": "explicit operator confirmation was not supplied",
                },
                severity="warning",
                moment=at,
                event_id=operation_id,
            )
            raise PermissionError("重启Quantclass前必须完成确认")
        if not self._manual_action_lock.acquire(blocking=False):
            raise RuntimeError("另一个人工操作正在执行，请稍后再试")
        try:
            now = datetime.now().astimezone()
            rocket = self.rocket_monitor.observe(now)
            operation_id = self._new_identifier("QGO", now)
            operation = {
                "operation_id": operation_id,
                "operation_type": "quantclass_restart",
                "initiator": "manual",
                "target_component": "trade_system.client",
                "context": "production",
                "started_at": now,
            }
            event_id = self.audit.record(
                "manual_quantclass_restart_requested",
                {
                    **operation,
                    "component_id": "trade_system.client",
                    "status": "in_progress",
                    "phase": "requested",
                    "operator_confirmed": True,
                    "automatic_recovery_gate_bypassed": True,
                    "rocket_active": rocket.active,
                    "selection_engine": self.config.trade_system.selection_engine,
                },
                severity="warning",
                moment=now,
                event_id=operation_id,
            )
            result = self.quantclass_controller.restart(event_id=event_id)
            completed_at = datetime.now().astimezone()
            self.audit.record(
                "manual_quantclass_restart_result",
                {
                    **operation,
                    "component_id": "trade_system.client",
                    "status": "succeeded" if result.success else "failed",
                    "phase": "completed",
                    "completed_at": completed_at,
                    "duration_ms": max(
                        0, int((completed_at - now).total_seconds() * 1000)
                    ),
                    "operator_confirmed": True,
                    "success": result.success,
                    "launched": result.launched,
                    "live_action": result.live_action,
                    "reason": result.reason,
                    "details": result.details,
                },
                severity="info" if result.success else "critical",
                moment=completed_at,
                event_id=event_id,
            )
            self.notifications.publish(
                "Quantclass人工重启完成" if result.success else "Quantclass人工重启失败",
                result.reason,
                severity="info" if result.success else "critical",
                event_key="manual_quantclass_restart_result",
                now=completed_at,
            )
            if not result.success:
                raise RuntimeError(result.reason)
            self.request_check()
            return self.status
        finally:
            self._manual_action_lock.release()

    def pause(self) -> ServiceStatus:
        at = datetime.now().astimezone()
        transition = self.machine.pause(at)
        self._record_transition(transition)
        self._record_control_operation("pause", at, transition.reason)
        self._wake_event.set()
        return self.run_once()

    def resume(self) -> ServiceStatus:
        at = datetime.now().astimezone()
        transition = self.machine.resume(at)
        self._record_transition(transition)
        self._record_control_operation("resume", at, transition.reason)
        self._wake_event.set()
        return self.run_once()

    def unlock(self) -> ServiceStatus:
        at = datetime.now().astimezone()
        transition = self.machine.unlock(at)
        self._record_transition(transition)
        self._record_control_operation("unlock", at, transition.reason)
        return self.run_once()

    def _record_control_operation(
        self,
        command: str,
        at: datetime,
        reason: str,
    ) -> None:
        operation_id = self._new_identifier("QGO", at)
        event_type = {
            "pause": "recovery_paused",
            "resume": "recovery_resumed",
            "unlock": "recovery_unlocked",
            "acknowledge": "recovery_acknowledged",
        }.get(command, "recovery_control_changed")
        self.audit.record(
            event_type,
            {
                "component_id": "qmt_api.recovery",
                "operation_id": operation_id,
                "operation_type": "recovery_control",
                "initiator": "manual",
                "target_component": "qmt_api.recovery",
                "context": "production",
                "command": command,
                "started_at": at,
                "completed_at": at,
                "status": "succeeded",
                "phase": "completed",
                "reason": reason,
            },
            severity="info",
            moment=at,
            event_id=operation_id,
        )

    def operator_check(
        self,
        source: str = "all",
        *,
        initiator: str = "manual",
        remote_channel: str = "",
        remote_request_id: str = "",
    ) -> ServiceStatus:
        """Run and audit an operator-requested read-only health check."""

        if source not in {"all", "qmt", "trade"}:
            raise ValueError(f"unsupported check source: {source}")
        if initiator not in {"manual", "remote_telegram", "remote_weixin"}:
            raise ValueError("unsupported check initiator")
        started_at = datetime.now().astimezone()
        operation_id = self._new_identifier("QGO", started_at)
        target = (
            "qmt_api"
            if source == "qmt"
            else "trade_system"
            if source == "trade"
            else "quant_guardian"
        )
        operation = {
            "operation_id": operation_id,
            "operation_type": "manual_check",
            "initiator": initiator,
            "target_component": target,
            "context": "production",
            "started_at": started_at,
        }
        if remote_channel:
            operation["remote_channel"] = remote_channel
        if remote_request_id:
            operation["remote_request_id"] = remote_request_id
        self.audit.record(
            "manual_check_requested",
            {
                **operation,
                "component_id": target,
                "source": source,
                "status": "in_progress",
                "phase": "requested",
                "reason": "operator requested an immediate read-only check",
            },
            severity="info",
            moment=started_at,
            event_id=operation_id,
        )
        try:
            status = self.run_once(wait_for_lock=True)
        except Exception as exc:
            completed_at = datetime.now().astimezone()
            self.audit.record(
                "manual_check_result",
                {
                    **operation,
                    "component_id": target,
                    "source": source,
                    "status": "failed",
                    "phase": "completed",
                    "success": False,
                    "completed_at": completed_at,
                    "duration_ms": max(
                        0,
                        int((completed_at - started_at).total_seconds() * 1000),
                    ),
                    "reason": f"{type(exc).__name__}: {exc}",
                },
                severity="warning",
                moment=completed_at,
                event_id=operation_id,
            )
            raise
        completed_at = datetime.now().astimezone()
        component = status.components.get(target, {})
        component_state = (
            str(component.get("state") or "unknown")
            if isinstance(component, dict)
            else "unknown"
        )
        self.audit.record(
            "manual_check_result",
            {
                **operation,
                "component_id": target,
                "source": source,
                "status": "succeeded",
                "phase": "completed",
                "success": True,
                "completed_at": completed_at,
                "duration_ms": max(
                    0, int((completed_at - started_at).total_seconds() * 1000)
                ),
                "observed_state": component_state,
                "guardian_state": status.state.value,
                "reason": f"read-only check completed: {component_state}",
            },
            severity="info",
            moment=completed_at,
            event_id=operation_id,
        )
        return status

    def acknowledge_manual(self) -> ServiceStatus:
        at = datetime.now().astimezone()
        transition = self.machine.acknowledge_manual(at)
        self._manual_after_recovery = False
        self._last_reconciliation = {
            **self._last_reconciliation,
            "acknowledged": True,
            "acknowledged_at": transition.at.isoformat(),
        }
        self._record_transition(transition)
        self._record_control_operation("acknowledge", at, transition.reason)
        current = self.status
        updated = ServiceStatus(
            state=transition.new_state,
            action=transition.action,
            reason=transition.reason,
            observed_at=transition.at,
            safety_live_actions=current.safety_live_actions,
            safety_reason=current.safety_reason,
            process=current.process,
            probe=current.probe,
            log=current.log,
            rocket=current.rocket,
            reconciliation=dict(self._last_reconciliation),
            business_summary=current.business_summary,
            components=current.components,
            attention=current.attention,
            schedule=current.schedule,
        )
        self._publish_status(updated)
        return updated

    def export_diagnostics(self, destination: Path) -> Path:
        started_at = datetime.now().astimezone()
        operation_id = self._new_identifier("QGO", started_at)
        exporter = DiagnosticExporter(self._runtime_root)
        path = exporter.export(destination, self.config)
        completed_at = datetime.now().astimezone()
        self.audit.record(
            "diagnostic_exported",
            {
                "component_id": "quant_guardian",
                "operation_id": operation_id,
                "operation_type": "diagnostic_export",
                "initiator": "manual",
                "target_component": "quant_guardian",
                "context": "production",
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": max(
                    0, int((completed_at - started_at).total_seconds() * 1000)
                ),
                "status": "succeeded",
                "phase": "completed",
                "destination": str(path),
                "raw_qmt_logs_included": False,
            },
            moment=completed_at,
            event_id=operation_id,
        )
        return path

    def record_settings_changed(self) -> None:
        at = datetime.now().astimezone()
        operation_id = self._new_identifier("QGO", at)
        self.audit.record(
            "settings_changed",
            {
                "component_id": "quant_guardian",
                "operation_id": operation_id,
                "operation_type": "settings_change",
                "initiator": "manual",
                "target_component": "quant_guardian",
                "context": "production",
                "started_at": at,
                "completed_at": at,
                "status": "succeeded",
                "phase": "completed",
                "mode": self.config.mode,
                "active_interval_seconds": (
                    self.config.monitoring.active_interval_seconds
                ),
                "idle_interval_seconds": self.config.monitoring.idle_interval_seconds,
                "allow_idle_recovery": (
                    self.config.monitoring.allow_idle_recovery
                ),
                "reason": "operator saved Quant Guardian settings",
            },
            severity="info",
            moment=at,
            event_id=operation_id,
        )

    def recent_events(self, limit: int = 100) -> list[dict[str, object]]:
        values = self.store.fetch_events(limit=limit)
        return values if values else self.audit.recent(limit)

    def query_events(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        search: str = "",
        severity: str = "all",
        component: str = "all",
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, object]]:
        values = self.store.fetch_events(
            limit=limit,
            offset=offset,
            search=search,
            severity=severity,
            component=component,
            since=since,
            until=until,
        )
        if (
            values
            or offset
            or search
            or severity != "all"
            or component != "all"
            or since is not None
            or until is not None
        ):
            return values
        return self.audit.recent(limit)

    def query_operations(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        since: datetime | None = None,
        until: datetime | None = None,
        operation_type: str = "all",
        initiator: str = "all",
        status: str = "all",
        context: str = "all",
        search: str = "",
    ) -> list[dict[str, object]]:
        return self.store.fetch_operations(
            limit=limit,
            offset=offset,
            since=since,
            until=until,
            operation_type=operation_type,
            initiator=initiator,
            status=status,
            context=context,
            search=search,
        )

    def operation_stats(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        context: str = "all",
    ) -> dict[str, object]:
        return self.store.fetch_operation_stats(
            since=since,
            until=until,
            context=context,
        )

    def operation_detail(self, operation_id: str) -> dict[str, object]:
        return self.store.fetch_operation_detail(operation_id)

    def trend_samples(
        self,
        *,
        since: datetime,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        return self.store.fetch_samples(
            since=since,
            limit=limit or self.config.monitoring.max_chart_points,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="quant-guardian-monitor",
            daemon=True,
        )
        self._thread.start()

    @property
    def monitor_thread_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def ensure_monitoring(self) -> bool:
        """Restart a dead background worker without touching QMT or Trade System."""
        if self._stop_event.is_set() or self.monitor_thread_alive:
            return False
        started_at = datetime.now().astimezone()
        operation_id = self._new_identifier("QGO", started_at)
        self.start()
        completed_at = datetime.now().astimezone()
        succeeded = self.monitor_thread_alive
        try:
            self.audit.record(
                "monitor_thread_restarted",
                {
                    "component_id": "quant_guardian",
                    "operation_id": operation_id,
                    "operation_type": "guardian_worker_restart",
                    "initiator": "watchdog",
                    "target_component": "quant_guardian.monitor_loop",
                    "context": "production",
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "duration_ms": max(
                        0,
                        int((completed_at - started_at).total_seconds() * 1000),
                    ),
                    "status": "succeeded" if succeeded else "failed",
                    "phase": "completed",
                    "success": succeeded,
                    "reason": "background monitor thread was not alive",
                },
                severity="warning" if succeeded else "critical",
                moment=completed_at,
                event_id=operation_id,
            )
        except Exception:
            pass
        return succeeded

    def _write_monitor_heartbeat(
        self,
        at: datetime,
        *,
        state: str,
        retry_at: datetime | None = None,
        error: str = "",
    ) -> None:
        document = {
            "schema_version": 1,
            "updated_at": at.isoformat(),
            "state": state,
            "thread_alive": self.monitor_thread_alive,
            "last_successful_check_at": (
                self._last_monitor_success_at.isoformat()
                if self._last_monitor_success_at
                else ""
            ),
            "consecutive_errors": self._monitor_consecutive_errors,
            "retry_at": retry_at.isoformat() if retry_at else "",
            "error": error,
        }
        try:
            temporary = self._monitor_heartbeat_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._monitor_heartbeat_path)
        except OSError:
            pass

    def _record_monitor_loop_error(
        self,
        exc: Exception,
        *,
        at: datetime,
        retry_seconds: float,
    ) -> None:
        self._monitor_consecutive_errors += 1
        retry_at = at + timedelta(seconds=retry_seconds)
        error = f"{type(exc).__name__}: {exc}"
        try:
            self.audit.record(
                "monitor_loop_error",
                {
                    "component_id": "quant_guardian.monitor_loop",
                    "error": error,
                    "traceback": traceback.format_exc(limit=12),
                    "consecutive_errors": self._monitor_consecutive_errors,
                    "retry_seconds": retry_seconds,
                    "last_successful_check_at": self._last_monitor_success_at,
                },
                severity="critical",
                moment=at,
            )
        except Exception:
            pass
        self._write_monitor_heartbeat(
            at,
            state="retrying",
            retry_at=retry_at,
            error=error,
        )
        current = self.status
        failed_status = replace(
            current,
            observed_at=at,
            reason="Guardian 监控循环异常，正在自动重试",
            attention={
                "required": True,
                "level": "critical",
                "title": "Guardian 监控正在自恢复",
                "message": error,
                "action": "等待自动重试",
                "target": "monitor",
            },
            schedule={
                **current.schedule,
                "interval_seconds": retry_seconds,
                "next_check_at": retry_at.isoformat(),
                "burst_reason": "monitor_loop_retry",
            },
        )
        with self._status_lock:
            self._status = failed_status
        for listener in tuple(self._listeners):
            try:
                listener(failed_status)
            except Exception:
                continue

    def _monitoring_gap_tolerance_seconds(self) -> float:
        scheduled_interval = self._expected_monitor_interval_seconds
        interval = (
            scheduled_interval
            if scheduled_interval is not None and scheduled_interval > 0
            else float(self.config.monitoring.active_interval_seconds)
        )
        return max(5.0, float(interval) * 2)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            loop_started = datetime.now().astimezone()
            expected = self._expected_monitor_check_at
            if expected is not None:
                delay = (loop_started - expected).total_seconds()
                tolerance = self._monitoring_gap_tolerance_seconds()
                if delay > tolerance:
                    self.audit.record(
                        "monitoring_gap",
                        {
                            "component_id": "quant_guardian",
                            "expected_at": expected.isoformat(),
                            "resumed_at": loop_started.isoformat(),
                            "delay_seconds": round(delay, 3),
                            "reason": "scheduled monitoring deadline was missed",
                        },
                        severity="warning",
                        moment=loop_started,
                    )
            try:
                status = self.run_once()
            except Exception as exc:
                at = datetime.now().astimezone()
                retry_seconds = max(
                    1.0,
                    float(self.config.monitoring.monitor_error_retry_seconds),
                )
                self._record_monitor_loop_error(
                    exc,
                    at=at,
                    retry_seconds=retry_seconds,
                )
                self._expected_monitor_interval_seconds = retry_seconds
                self._expected_monitor_check_at = at + timedelta(
                    seconds=retry_seconds
                )
                self._wake_event.wait(retry_seconds)
                self._wake_event.clear()
                continue
            self._monitor_consecutive_errors = 0
            self._last_monitor_success_at = status.observed_at
            self._write_monitor_heartbeat(status.observed_at, state="healthy")
            next_value = status.schedule.get("next_check_at")
            try:
                next_check_at = datetime.fromisoformat(str(next_value))
                if next_check_at.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                interval = status.schedule.get("interval_seconds")
                seconds = (
                    float(interval)
                    if isinstance(interval, (int, float))
                    else self.config.monitoring.active_interval_seconds
                )
                next_check_at = datetime.now().astimezone() + timedelta(
                    seconds=seconds
                )
            interval = status.schedule.get("interval_seconds")
            try:
                scheduled_seconds = float(interval)
            except (TypeError, ValueError):
                scheduled_seconds = max(
                    0.1,
                    (next_check_at - datetime.now().astimezone()).total_seconds(),
                )
            self._expected_monitor_interval_seconds = max(0.1, scheduled_seconds)
            self._expected_monitor_check_at = next_check_at
            seconds = (next_check_at - datetime.now().astimezone()).total_seconds()
            self._wake_event.wait(max(0.1, seconds))
            self._wake_event.clear()

    def request_check(self) -> None:
        self.ensure_monitoring()
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._write_monitor_heartbeat(
            datetime.now().astimezone(), state="stopped"
        )
        self.business.stop()
        self.probe.stop()
        self.store.request_cleanup()
        self.store.close()
