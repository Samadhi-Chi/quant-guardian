# v0.6.0-beta.1 — Quantclass 5 execution-mode adaptation

This release replaces miniQMT-only assumptions with a discovered execution contract. It preserves existing schema-v2 configuration, encrypted credentials, audit history and QMT-only automatic recovery authority.

## Mode behavior

| Mode | Evidence | Recovery |
|---|---|---|
| miniQMT | Exact XtMiniQmt process, independent XTQuant, account and counts | Existing controlled recovery gates |
| Plan A / broker-invited | Exact XtItClient process; broker-bundled Python/XTQuant against userdata | Same gates; old process must be confirmed closed |
| Plan X / native plan | Exact XtItClient process and dated generated plans | Manual restart only; account/execution/fills remain unverified |
| Legacy WebSocket / unknown | Visible unsupported capability | No automatic API-driven restart |

Settings default to following the saved Quantclass mode. Switching modes invalidates old worker sessions and samples. An incomplete JSON write retains the last mode but suspends recovery until a valid config is readable. No third-party settings are changed.

## Status and evidence

- Status shows the detected mode, active query capability, execution readiness, native-plan delivery and strategy handoff.
- Intentionally disabled auto-trading and Plan X do not produce a missing-Rocket fault.
- A failed client connection-check flag remains actionable even if an independent QMT query succeeds: Client 5.0 uses that flag to decide whether to wake Rocket.
- Query results are separate: a missing trade callback is `unknown`, never zero. Returned order/position counts remain visible. No account, security, price or amount is returned by the broker helper.
- Native-plan generation is tracked separately from selection. Failure of an optional native script does not invalidate a successful Plan A selection task.
- Nonzero ledger rows whose strategy tags are no longer configured produce a handoff warning. This is a label-level ledger check, not a broker position reconciliation, and never initiates trading.
- The health timeline retains warning/critical/idle distinctions. The core-normal-sample metric covers Fuel, selection and order state only; it is not a task, order or fill success rate, and handoff warnings do not turn it into a false 0% execution result.
- The minute-data sessions follow Client 5.0's 09:16–11:26 and 13:01–15:01 windows, Asia/Shanghai. Daily plans are checked by their intended trading date, so a valid prior-evening plan is not rejected merely because it is old in hours.

## Recovery and self-monitoring

Interface disablement, account authorization requirements, a broken helper environment and unknown process identity cannot request QMT restarts. State-machine tests cover these states. Main-window close targets the selected mode's main process. If close/termination does not finish, a duplicate launch is blocked.

Launcher existence, working directory and embedded administrator requirements are checked **before closing QMT**. A limited Guardian cannot stop an admin-required QMT and then leave it unable to relaunch. Elevation requires the user's explicit `install-task.ps1 -RunElevated` action from an administrator shell; Guardian never tries to bypass UAC or silently changes its startup privilege.

The existing independent recovery sentinel remains required; this upgrade neither creates it nor expands authority to Quantclass or its engines. Manual restart still requires the existing confirmation dialog.

The scheduled task runs the new Guardian-only watchdog. Normal hourly gaps are respected. Missing Guardian is relaunched; a running but stale Guardian requires persistent confirmation plus heartbeat/index evidence, then a fresh sample is verified after relaunch. Results appear in the operation history.

## Validation boundary

Read-only Plan A connectivity is verified on the local broker build listed in COMPATIBILITY.md. Recovery lifecycle and failure controls are exercised with fake processes; no live trading app is restarted as part of automated tests. Plan X still needs human confirmation of native strategy execution. Future vendor versions and other brokers remain unverified.
