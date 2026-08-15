# Safety model

Quant Guardian treats monitoring and recovery as different privileges.

## Invariants

1. Automatic recovery targets QMT only.
2. Quantclass, Fuel, Aqua, Zeus and Rocket are never automatically started, stopped or repaired.
3. Trade System-only failures cannot trigger QMT recovery.
4. A log line alone cannot trigger recovery.
5. External network failure cannot trigger recovery.
6. Process termination requires a fresh PID, creation-time and executable-path match.
7. A manual restart bypasses observation-mode authorization only after an operator confirmation; it does not bypass identity checks or the operation lock.
8. Order submission and cancellation APIs are not part of the read-only adapter.

## Automatic recovery authorization

All of the following must be true:

- configuration mode is recover;
- automatic_recovery_until is absent or still valid;
- the local RECOVERY_ENABLED file exists and its complete content matches the expected sentinel;
- the current schedule permits recovery;
- Rocket is not active unless the explicit compatibility option is enabled;
- rate limits, backoff and lockout allow another attempt.

Removing the sentinel or returning to observe mode revokes automatic actions immediately.

## Evidence policy

QMT API parent health is based on:

- exact QMT process state;
- XTQuant session result;
- account login/read-only query result.

Low-frequency order/trade/position counts are display evidence only. Their timeout degrades the summary without restarting QMT.

Trade System health is evaluated independently:

- Fuel uses recent task result and data freshness.
- Aqua or Zeus uses the configured selection engine's last result and output freshness.
- Rocket uses its own process and incremental log heartbeat.

Batch components may be idle. Absence of a continuously running process outside its expected task is not automatically a fault.

## Recovery limits

- confirmation burst: three consistent failures within 45 seconds by default;
- graceful close before exact termination;
- bounded attempts per 30 minutes and per day;
- escalating backoff;
- consecutive success verification over a minimum span;
- manual-required and lockout terminal states.

After a QMT restart, Rocket activity requires operator reconciliation when configured. Quant Guardian reports counts only and does not infer that a plan became an order or that an order became a fill.

## Privacy

Audit records avoid securities, prices, values and raw account identifiers. Diagnostic exports additionally replace:

- user profile, AppData and temporary roots;
- configured QMT root;
- configured Quantclass root;
- account-like identifiers, passwords, tokens, secrets and webhook credentials.

Always inspect a diagnostic ZIP before sharing it.
