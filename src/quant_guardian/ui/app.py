from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from quant_guardian.config import AppConfig, default_config_path, ensure_runtime_directories
from quant_guardian.gateway.bridge import GatewayEventBridge
from quant_guardian.gateway.config import (
    default_messaging_config_path,
    load_messaging_config,
    save_messaging_config,
)
from quant_guardian.gateway.ipc import GuardianControlServer
from quant_guardian.gateway.secrets import CredentialVault
from quant_guardian.gateway.store import GatewayStore
from quant_guardian.gateway.supervisor import GatewaySupervisor
from quant_guardian.service import GuardianService
from quant_guardian.ui.design_system import install_ui_font
from quant_guardian.ui.main_window import MainWindow


def run_gui(
    config: AppConfig,
    config_path: Path | None = None,
    *,
    start_monitoring: bool = True,
    start_gateway: bool = True,
    auto_quit_ms: int | None = None,
    show_onboarding: bool = False,
    runtime_root: Path | None = None,
) -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("Quant Guardian")
    application.setOrganizationName("Quant Guardian")
    application.setStyle("Fusion")
    application.setFont(QFont(install_ui_font(), 10))
    application.setQuitOnLastWindowClosed(False)

    if runtime_root is None:
        paths = ensure_runtime_directories()
    else:
        root = runtime_root.resolve()
        paths = {
            "root": root,
            "config": root / "config",
            "state": root / "state",
            "logs": root / "logs",
            "diagnostics": root / "diagnostics",
            "cache": root / "cache",
            "secrets": root / "secrets",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(paths["state"] / "quant-guardian.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(
            None,
            "Quant Guardian 已在运行",
            "已有一个 Quant Guardian 实例正在运行。请检查系统托盘。",
        )
        return 2

    service = GuardianService(config)
    messaging_path = (
        default_messaging_config_path()
        if runtime_root is None
        else paths["config"] / "messaging.json"
    )
    if messaging_path.exists():
        messaging_config = load_messaging_config(messaging_path)
    else:
        messaging_config = load_messaging_config(None)
        save_messaging_config(messaging_config, messaging_path)
    gateway_store = GatewayStore(
        None if runtime_root is None else paths["state"] / "gateway.db"
    )
    gateway_vault = CredentialVault(
        None if runtime_root is None else paths["secrets"] / "messaging-secrets.json"
    )
    gateway_bridge = GatewayEventBridge(
        gateway_store,
        config_path=messaging_path,
    )
    service.notifications.subscribe(gateway_bridge.on_notification)
    service.audit.subscribe(gateway_bridge.on_audit)
    control_address = None
    if runtime_root is not None:
        digest = hashlib.sha256(str(paths["root"]).encode("utf-8")).hexdigest()[:12]
        control_address = (
            rf"\\.\pipe\quant-guardian-smoke-{os.getpid()}-{digest}"
            if os.name == "nt"
            else str(paths["state"] / f"control-{digest}.sock")
        )
    control_server = GuardianControlServer(
        service,
        messaging_path=messaging_path,
        vault=gateway_vault,
        store=gateway_store,
        address=control_address,
        sentinel_path=(
            None if runtime_root is None else paths["state"] / "REMOTE_CONTROL_ENABLED"
        ),
    )
    try:
        control_server.start()
    except RuntimeError as exc:
        service.audit.record(
            "gateway_control_server_failed",
            {
                "component_id": "quant_guardian.messaging",
                "reason": str(exc),
            },
            severity="warning",
        )
    if (
        start_gateway
        and messaging_config.gateway_enabled
        and messaging_config.autostart
    ):
        try:
            GatewaySupervisor(messaging_path).start()
        except OSError as exc:
            service.audit.record(
                "gateway_process_start_failed",
                {
                    "component_id": "quant_guardian.messaging",
                    "reason": f"{type(exc).__name__}: {exc}",
                },
                severity="warning",
            )
    window = MainWindow(
        service,
        config,
        config_path or default_config_path(),
        show_onboarding=show_onboarding,
        messaging_config_path=messaging_path,
        gateway_store=gateway_store,
        credential_vault=gateway_vault,
    )
    application.aboutToQuit.connect(control_server.stop)
    application.aboutToQuit.connect(service.stop)
    window.show()
    if start_monitoring:
        service.start()
        watchdog = QTimer(application)
        watchdog.setInterval(30_000)
        watchdog.timeout.connect(service.ensure_monitoring)
        watchdog.start()
    if auto_quit_ms is not None:
        QTimer.singleShot(auto_quit_ms, application.quit)
    result = application.exec()
    control_server.stop()
    service.stop()
    lock.unlock()
    return result
