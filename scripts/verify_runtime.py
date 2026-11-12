"""Verify discovered runtime with observation-only, isolated Guardian state."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from quant_guardian.config import default_config_path, load_config
from quant_guardian.service import GuardianService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    config.mode = "observe"
    config.notifications.desktop_enabled = False
    with tempfile.TemporaryDirectory(prefix="quant-guardian-readonly-check-") as temporary:
        service = GuardianService(config, runtime_root=Path(temporary))
        try:
            first = service.run_once()
            deadline = time.monotonic() + 12
            while service.business.latest.get("status") == "pending" and time.monotonic() < deadline:
                time.sleep(0.1)
            result = {
                "read_only": True, "live_recovery_allowed": False,
                "integration": first.integration,
                "process": first.process, "probe": first.probe,
                "business": service.business.latest,
                "components": first.components, "attention": first.attention,
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
