# Development and release validation

## Environment

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -Dev
~~~

The main source supports Python 3.11–3.14 x64. The public Windows build uses Python 3.14.6; the XTQuant worker remains external and uses Python 3.11.

## Required local checks

~~~powershell
.\.venv\Scripts\python.exe -m pytest --cov=quant_guardian --cov-report=term-missing --cov-report=json
.\.venv\Scripts\python.exe .\scripts\check_coverage.py coverage.json
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m bandit -c pyproject.toml -r src -ll
.\.venv\Scripts\python.exe -m pip_audit -r requirements-lock.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-install-safety.ps1
~~~

Tests must cover PID reuse, path mismatch, recovery authorization, QMT-only automatic behavior, diagnostic privacy and release ZIP contents.

## Simulated UI screenshots

~~~powershell
.\.venv\Scripts\python.exe .\scripts\render_ui_preview.py .\preview-output
~~~

Only synthetic data from PreviewService may be committed. Do not capture a live account, PID, path or operational log.

## Build

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-release.ps1 -Version 0.3.0b1 -ReleaseTag v0.3.0-beta.1
~~~

The package script verifies the tag/package mapping, license inventory, forbidden content and SHA-256. It creates the Windows ZIP, CycloneDX SBOM and checksum file under release-assets.

## Release acceptance

Use a clean isolated directory. Validate:

- Quant Guardian.exe --version;
- --simulate;
- --ui-smoke;
- installer marker and uninstaller WhatIf/rollback;
- ZIP deny-list and required licenses;
- SBOM validity and ZIP checksum.

Do not replace a live trading installation during release acceptance.
