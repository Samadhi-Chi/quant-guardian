from __future__ import annotations

import json
import platform
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from quant_guardian import __version__
from quant_guardian.config import AppConfig
from quant_guardian.diagnostics.redaction import PathRedactor, redact, redact_text


class DiagnosticExporter:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root

    def export(self, destination: Path, config: AppConfig) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        path_redactor = PathRedactor.from_config(config)
        metadata = path_redactor.redact(
            redact(
            {
                "generated_at": datetime.now().astimezone().isoformat(),
                "quant_guardian_version": __version__,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "config": asdict(config),
                "raw_qmt_logs_included": False,
            }
            )
        )
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7
        ) as archive:
            archive.writestr(
                "metadata.json",
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            )
            log_directory = self.runtime_root / "logs"
            for path in sorted(log_directory.glob("guardian-*.jsonl"))[-7:]:
                try:
                    sanitized = path_redactor.redact_text(
                        redact_text(path.read_text(encoding="utf-8-sig"))
                    )
                except OSError:
                    continue
                archive.writestr(f"logs/{path.name}", sanitized)
        temporary.replace(destination)
        return destination
