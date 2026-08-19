from __future__ import annotations

import argparse
import json
from pathlib import Path

CRITICAL_MINIMUMS = {
    "src/quant_guardian/recovery/controller.py": 80.0,
    "src/quant_guardian/recovery/quantclass_controller.py": 80.0,
    "src/quant_guardian/recovery/windows_process_control.py": 80.0,
    "src/quant_guardian/safety.py": 80.0,
    "src/quant_guardian/security/dpapi.py": 80.0,
    "src/quant_guardian/diagnostics/redaction.py": 80.0,
    "src/quant_guardian/diagnostics/exporter.py": 80.0,
    "src/quant_guardian/gateway/channels/https.py": 80.0,
    "src/quant_guardian/gateway/commands.py": 80.0,
    "src/quant_guardian/gateway/config.py": 80.0,
    "src/quant_guardian/gateway/ipc.py": 80.0,
    "src/quant_guardian/gateway/secrets.py": 80.0,
    "src/quant_guardian/gateway/store.py": 80.0,
}


def _normalized_files(document: dict[str, object]) -> dict[str, dict[str, object]]:
    files = document.get("files", {})
    assert isinstance(files, dict)
    return {
        str(Path(name).as_posix()).casefold(): value
        for name, value in files.items()
        if isinstance(value, dict)
    }


def validate(path: Path) -> list[str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    failures: list[str] = []
    total = float(document["totals"]["percent_covered"])
    if total < 75.0:
        failures.append(f"total coverage {total:.2f}% is below 75%")
    files = _normalized_files(document)
    for expected, minimum in CRITICAL_MINIMUMS.items():
        match = next(
            (value for name, value in files.items() if name.endswith(expected.casefold())),
            None,
        )
        if match is None:
            failures.append(f"critical module missing from coverage report: {expected}")
            continue
        percent = float(match["summary"]["percent_covered"])
        if percent < minimum:
            failures.append(
                f"{expected} coverage {percent:.2f}% is below {minimum:.0f}%"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage_json", type=Path)
    args = parser.parse_args()
    failures = validate(args.coverage_json)
    if failures:
        raise SystemExit("\n".join(failures))
    print("coverage gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
