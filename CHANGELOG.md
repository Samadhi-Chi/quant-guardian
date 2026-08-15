# Changelog

All notable changes are documented here. Versions follow semantic versioning; pre-release identifiers use the corresponding Python package form internally.

## [0.3.0-beta.1] - 2026-08-15

First public preview.

### Added

- Windows 11-style Status, Monitoring and Settings views.
- Hierarchical QMT API and Trade System health model.
- QMT-only controlled automatic recovery with evidence confirmation, limits and lockout.
- SQLite-backed trends, events, recovery statistics and operation details.
- Install marker, guarded uninstaller and WhatIf support.
- Path-redacted diagnostic export.
- Windows release ZIP, SBOM and SHA-256 manifest workflow.

### Security

- Exact PID, creation-time and executable-path checks before termination.
- SHA-256 plus Authenticode verification for the isolated Python 3.11 XTQuant runtime installer.
- Release content allow/deny validation and automated security scanning.

[0.3.0-beta.1]: https://github.com/Samadhi-Chi/quant-guardian/releases/tag/v0.3.0-beta.1
