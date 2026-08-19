from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from quant_guardian.gateway.config import default_messaging_config_path


class GatewaySupervisor:
    """Launches the isolated gateway without granting it process-control APIs."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_messaging_config_path()

    @staticmethod
    def executable() -> Path | None:
        if getattr(sys, "frozen", False):
            candidate = Path(sys.executable).with_name("Quant Guardian Gateway.exe")
            return candidate if candidate.is_file() else None
        return Path(sys.executable)

    def command(self) -> list[str]:
        executable = self.executable()
        if executable is None:
            raise FileNotFoundError("Quant Guardian Gateway executable is missing")
        if getattr(sys, "frozen", False):
            return [str(executable), "--config", str(self.config_path)]
        return [
            str(executable),
            "-m",
            "quant_guardian.gateway.cli",
            "--config",
            str(self.config_path),
        ]

    def start(self) -> int:
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(
            self.command(),
            cwd=str(Path(sys.executable).parent),
            close_fds=True,
            creationflags=flags,
        )
        return int(process.pid)
