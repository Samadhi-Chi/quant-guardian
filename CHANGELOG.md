# Changelog

All notable changes are documented here. Versions follow semantic versioning; pre-release identifiers use the corresponding Python package form internally.

## [0.5.0-beta.1] - 2026-09-06

Quantclass Client 4.2.1 compatibility preview.

### Added

- Fusion selection-engine monitoring through exact processes, incremental logs and daily UI status files.
- Automatic selection-engine detection with explicit Fusion, Zeus and Aqua compatibility overrides.
- Safe in-memory migration of legacy v0.4 Zeus defaults when a Fusion installation is present.
- Fusion task result, status-file and detection-source evidence in the hierarchical Trade System status.

### Changed

- Updated the tested Quantclass baseline to Client 4.2.1 with Fusion 3.0.2/3.0.2a while retaining Client 4.1.1 compatibility.
- Updated the Windows UI, monitoring chart labels, configuration example and documentation for Fusion.
- Added Fusion and SCM to the remote-control deny list; automatic recovery remains QMT-only.

## [0.4.0-beta.1] - 2026-08-19

Messaging gateway preview.

### Added

- Separate `Quant Guardian Gateway.exe` process for Telegram Bot API and personal WeChat iLink text messaging.
- Durable message outbox, delivery statistics, channel health and remote-command audit views.
- Private-chat pairing, fixed read-only commands, Telegram button confirmation and WeChat one-time text confirmation.
- Independently authorized QMT-only remote controlled restart with fresh network, Rocket, login and recovery-lock checks.
- Settings pages for channels, broadcast rules, remote control and security audit.

### Security

- DPAPI-protected channel credentials and HMAC-authenticated local IPC.
- One private owner per channel; group control, arbitrary commands, trading actions and all Quantclass engine control remain unavailable.
- Nonce, expiry, idempotency, rate limits, durable confirmation challenges and message/path redaction.
- Trusted HTTPS endpoint restriction for personal WeChat iLink credentials.

## [0.3.0-beta.1] - 2026-08-15

First public preview.

### Added

- Windows 11-style Status, Monitoring and Settings views.
- Hierarchical QMT API and Trade System health model.
- QMT-only controlled automatic recovery with evidence confirmation, limits and lockout.
- SQLite-backed trends, events, recovery statistics and operation details.
- Install marker, guarded uninstaller and WhatIf support.
- Path-redacted diagnostic export.
- Closed-market XTQuant timeouts render as idle when the QMT process and network are healthy.
- Windows release ZIP, SBOM and SHA-256 manifest workflow.

### Security

- Exact PID, creation-time and executable-path checks before termination.
- SHA-256 plus Authenticode verification for the isolated Python 3.11 XTQuant runtime installer.
- Release content allow/deny validation and automated security scanning.

[0.3.0-beta.1]: https://github.com/Samadhi-Chi/quant-guardian/releases/tag/v0.3.0-beta.1
[0.4.0-beta.1]: https://github.com/Samadhi-Chi/quant-guardian/releases/tag/v0.4.0-beta.1
[0.5.0-beta.1]: https://github.com/Samadhi-Chi/quant-guardian/releases/tag/v0.5.0-beta.1
