from __future__ import annotations

import argparse
import json
from pathlib import Path


def actionable_results(path: Path) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if document.get("version") != "2.1.0":
        raise ValueError("unsupported SARIF version")
    findings: list[dict[str, object]] = []
    for run in document.get("runs", []):
        if not isinstance(run, dict):
            continue
        for result in run.get("results", []):
            if not isinstance(result, dict):
                continue
            properties = result.get("properties") or {}
            if isinstance(properties, dict) and properties.get("kind") in {
                "diagnostic",
                "metric",
            }:
                continue
            findings.append(result)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sarif", type=Path)
    args = parser.parse_args()
    findings = actionable_results(args.sarif)
    if findings:
        rule_ids = sorted({str(item.get("ruleId") or "unknown") for item in findings})
        raise SystemExit(
            f"CodeQL SARIF contains {len(findings)} actionable result(s): "
            + ", ".join(rule_ids)
        )
    print("CodeQL SARIF gate passed: 0 actionable results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
