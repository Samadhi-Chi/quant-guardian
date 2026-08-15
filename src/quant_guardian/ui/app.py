from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from quant_guardian.config import AppConfig, default_config_path, ensure_runtime_directories
from quant_guardian.service import GuardianService
from quant_guardian.ui.design_system import install_ui_font
from quant_guardian.ui.main_window import MainWindow


def run_gui(
    config: AppConfig,
    config_path: Path | None = None,
    *,
    start_monitoring: bool = True,
    auto_quit_ms: int | None = None,
    show_onboarding: bool = False,
) -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("Quant Guardian")
    application.setOrganizationName("Quant Guardian")
    application.setStyle("Fusion")
    application.setFont(QFont(install_ui_font(), 10))
    application.setQuitOnLastWindowClosed(False)

    paths = ensure_runtime_directories()
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
    window = MainWindow(
        service,
        config,
        config_path or default_config_path(),
        show_onboarding=show_onboarding,
    )
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
    service.stop()
    lock.unlock()
    return result
