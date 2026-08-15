# Troubleshooting

## The status is critical on a weekend or exchange holiday

Check the component:

- A broker session unavailable while QMT processes are healthy should be shown as idle outside a trading day.
- Missing or crashed QMT processes remain a real fault even on a closed day.
- Fuel, Aqua, Zeus or Rocket may be idle between scheduled jobs.

Verify the schedule mode and calendar source in the status header. Manual open/closed overrides take precedence.

## XTQuant probe is unavailable

1. Confirm the private Python path and XTQuant parent path in Settings.
2. Run one read-only check with --once.
3. Check that the installed XTQuant build matches the Python 3.11 ABI.
4. Do not copy XTQuant into the Quant Guardian Release directory.

A business-summary timeout alone must not restart QMT.

## Restart was blocked

The event and operation details show the reason. Common safety blocks include:

- observation mode or missing/expired sentinel;
- Rocket active;
- external network failure;
- identity mismatch or PID reuse;
- rate limit, backoff or lockout;
- QMT login requiring human interaction.

Do not weaken identity validation to make a restart pass.

## Windows confirmation dialog prevents unattended QMT startup

Quant Guardian can launch the configured official client, but it does not click through broker login, consent, CAPTCHA, update or risk-warning dialogs. Such a recovery ends in manual-required state.

## Export diagnostics

Use the Settings page export action. The ZIP contains redacted metadata and up to seven Guardian JSONL logs; raw QMT logs are excluded. Inspect the archive before sharing and use a private security report for vulnerabilities.

## UI or event page feels slow

Events are indexed in SQLite WAL mode and loaded in pages. If the index is damaged, close the app, preserve JSONL logs, and allow the index to rebuild. Do not delete live audit logs while investigating.
