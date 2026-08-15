# Architecture

Quant Guardian separates observation, decision and action so that a malformed log or unrelated Trade System failure cannot directly restart QMT.

## Components

~~~text
Qt UI / headless CLI
        |
GuardianService + scheduler
        |
        +-- QMT API evidence
        |     +-- exact process identity
        |     +-- isolated XTQuant read-only probe
        |     +-- account health and low-frequency counts
        |
        +-- Trade System evidence
        |     +-- Fuel data freshness
        |     +-- selected Aqua or Zeus result
        |     +-- Rocket process/log heartbeat
        |
        +-- state machine + SafetyGate
        |     +-- observe / suspect / degraded
        |     +-- recover / verify / manual / lockout
        |
        +-- QMT-only RecoveryController
        |
        +-- immutable JSONL audit + SQLite read index
~~~

## Trust boundaries

- QMT and Quantclass installations are external, untrusted inputs.
- The XTQuant native package is loaded only by an isolated Python 3.11 worker.
- Logs and status JSON are explanatory evidence; parsing errors retain the previous valid result.
- SQLite is a rebuildable index. JSONL remains the diagnostic record.
- UI requests execute service operations on worker threads; event and chart loading never synchronously scans the full log on the UI thread.

## QMT recovery sequence

1. QMT process, XTQuant and account evidence is sampled.
2. A critical failure is confirmed three times within the configured burst window.
3. Network failure, Trade System-only failure and log-only evidence are excluded.
4. SafetyGate verifies recovery mode, expiry and local sentinel.
5. The controller rescans exact processes and validates PID, creation time and executable path.
6. It requests graceful close, waits, and terminates only identities that still match.
7. It launches the configured official QMT executable.
8. Stable process, XTQuant and account success is required before the incident is resolved.

Quantclass recovery is a separate operator-only controller. It is never called by the automatic state machine and never targets Fuel, Aqua, Zeus or Rocket.

## Scheduling

- Confirmed trading day, 08:30–16:30 Asia/Shanghai: 5-second health checks.
- Other periods: hourly checks.
- An idle-period failure triggers a 15-second confirmation burst; it does not wait another hour.
- Manual calendar overrides have the highest priority.

## Data retention

Runtime data lives under %LOCALAPPDATA%\QuantGuardian by default and is excluded from source and release assets. Diagnostic export applies structured key redaction and path placeholders before writing a ZIP.
