# Contributing

Thank you for helping improve Quant Guardian.

## Before opening a change

- Use simulated or fully redacted data.
- Never commit account identifiers, orders, holdings, logs, QMT/Quantclass paths, tokens or proprietary binaries.
- Keep automatic actions limited to QMT. Changes that automatically start, stop or repair Quantclass, Fuel, Aqua, Zeus or Rocket are outside the project's safety model.
- Open a security report privately according to [SECURITY.md](SECURITY.md).

## Development

Use Windows x64 with Python 3.11–3.14:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest --cov=quant_guardian
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m bandit -c pyproject.toml -r src -ll
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-install-safety.ps1
~~~

Pull requests should explain the failure mode, safety impact, tests and rollback path. UI changes should include screenshots generated with scripts/render_ui_preview.py; only simulated data may be committed.

By submitting a contribution, you agree that it is licensed under Apache-2.0.
