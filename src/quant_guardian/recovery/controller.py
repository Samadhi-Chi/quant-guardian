from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from quant_guardian.config import AppConfig
from quant_guardian.diagnostics.audit import AuditLogger
from quant_guardian.domain.models import HealthSnapshot, ProcessStatus, RecoveryResult
from quant_guardian.monitors.process_monitor import QmtProcessMonitor
from quant_guardian.recovery.windows_process_control import (
    request_graceful_close,
    terminate_exact,
    wait_for_exit,
)
from quant_guardian.safety import SafetyGate


class RecoveryController:
    def __init__(
        self,
        config: AppConfig,
        process_monitor: QmtProcessMonitor,
        safety_gate: SafetyGate,
        audit: AuditLogger,
    ) -> None:
        self.config = config
        self.process_monitor = process_monitor
        self.safety_gate = safety_gate
        self.audit = audit
        self._lock = threading.Lock()

    def _launch(self) -> tuple[bool, str]:
        launcher = Path(self.config.qmt.launcher)
        working_directory = Path(self.config.qmt.working_directory)
        if not launcher.is_file():
            return False, f"QMT launcher does not exist: {launcher}"
        if not working_directory.is_dir():
            return False, f"QMT working directory does not exist: {working_directory}"
        try:
            subprocess.Popen(
                [str(launcher)],
                cwd=str(working_directory),
                close_fds=True,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except OSError as exc:
            return False, f"failed to start QMT launcher: {exc}"
        return True, "QMT official launcher started"

    def recover(
        self,
        snapshot: HealthSnapshot,
        *,
        event_id: str,
    ) -> RecoveryResult:
        return self._recover(snapshot, event_id=event_id, require_automatic_gate=True)

    def restart_manually(
        self,
        snapshot: HealthSnapshot,
        *,
        event_id: str,
    ) -> RecoveryResult:
        """Run an operator-confirmed restart without the automatic-mode gate.

        Process identity checks, the non-blocking operation lock, exact
        termination and configured-launcher validation remain mandatory.
        """

        return self._recover(snapshot, event_id=event_id, require_automatic_gate=False)

    def _recover(
        self,
        snapshot: HealthSnapshot,
        *,
        event_id: str,
        require_automatic_gate: bool,
    ) -> RecoveryResult:
        if require_automatic_gate:
            if (
                snapshot.rocket_active
                and not self.config.recovery.allow_qmt_restart_while_rocket_active
            ):
                return RecoveryResult(
                    False,
                    False,
                    False,
                    "Rocket is active; automatic QMT recovery is suppressed",
                )
            gate = self.safety_gate.status()
            if not gate.live_actions_allowed:
                return RecoveryResult(False, False, False, gate.reason)
        if not self._lock.acquire(blocking=False):
            return RecoveryResult(
                False, False, True, "another recovery is already running"
            )
        try:
            observation = self.process_monitor.observe()
            if observation.status is ProcessStatus.IDENTITY_MISMATCH:
                return RecoveryResult(
                    False,
                    False,
                    True,
                    "process identity mismatch; refusing to terminate by name",
                )
            identities = self.process_monitor.validated_processes()
            self.audit.record(
                "recovery_snapshot",
                {
                    "event_id": event_id,
                    "snapshot": snapshot,
                    "operator_confirmed": not require_automatic_gate,
                    "validated_process_count": len(identities),
                    "validated_pids": [identity.pid for identity in identities],
                },
                severity="warning",
                moment=datetime.now().astimezone(),
                event_id=event_id,
            )
            if identities:
                main_pids = {
                    identity.pid
                    for identity in identities
                    if identity.name.casefold() == "xtminiqmt.exe"
                }
                request_graceful_close(main_pids)
                remaining = wait_for_exit(
                    identities,
                    timeout=self.config.recovery.graceful_close_seconds,
                )
                if remaining:
                    terminate_exact(
                        remaining,
                        Path(self.config.qmt.launcher).parent,
                    )
            launched, reason = self._launch()
            return RecoveryResult(
                success=launched,
                launched=launched,
                live_action=True,
                reason=reason,
                details={"validated_process_count": len(identities)},
            )
        finally:
            self._lock.release()
