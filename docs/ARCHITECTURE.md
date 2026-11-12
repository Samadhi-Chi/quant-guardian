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
        |     +-- runtime profile: miniQMT / Plan A / Plan X
        |     +-- isolated XTQuant or broker-runtime read-only probe
        |     +-- account health and low-frequency counts
        |
        +-- Trade System evidence
        |     +-- Fuel data freshness
        |     +-- auto-detected Fusion or compatible Aqua/Zeus result
        |     +-- Rocket process/log heartbeat
        |     +-- client readiness / native plan delivery / legacy ledger handoff
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
- miniQMT loads XTQuant only in an isolated Python 3.11 worker. Plan A loads the broker-bundled ABI in the broker's Python 3.6+ child process using private correlated file IPC. Neither worker exposes order, cancellation or transfer calls.
- Plan X and unsupported bridge modes never invoke a fallback miniQMT probe. API permission removal, an unreadable client config and an unverifiable process identity require manual handling, not repeated restarts.
- Runtime-mode changes discard previous probe sessions and samples but preserve recovery limits and lockout.
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
7. It confirms the old process has exited before launching the configured official QMT executable. Failure to close an elevated process blocks a duplicate launch.
8. Stable process, XTQuant and account success is required before the incident is resolved.

Quantclass recovery is a separate operator-only controller. It is never called by the automatic state machine and never targets Fuel, Fusion, SCM, Aqua, Zeus or Rocket.

## Scheduling

- Confirmed trading day, 08:30–16:30 Asia/Shanghai: 5-second health checks.
- Other periods: hourly checks.
- An idle-period failure triggers a 15-second confirmation burst; it does not wait another hour.
- Manual calendar overrides have the highest priority.

The separate scheduled `--watchdog` entry point checks Guardian's exact executable, heartbeat and indexed latest sample. It observes the current active/hourly cadence, confirms persistent staleness for 90 seconds before terminating Guardian, and verifies a new real sample after launch. It cannot target QMT or Quantclass. The in-process 30-second watchdog remains a faster first line for a failed monitor thread.

## Data retention

Runtime data lives under %LOCALAPPDATA%\QuantGuardian by default and is excluded from source and release assets. Diagnostic export applies structured key redaction and path placeholders before writing a ZIP.
