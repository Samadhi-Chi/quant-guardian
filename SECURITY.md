# Security policy

## Supported versions

Security fixes are provided for the newest GitHub pre-release or stable release only.

## Report a vulnerability privately

Please use GitHub **Private vulnerability reporting** on the repository Security page. Do not open a public Issue for a suspected vulnerability.

Include:

- affected Quant Guardian version and Windows version;
- a minimal reproduction using simulated or redacted data;
- impact and the security boundary crossed;
- suggested mitigation, if known.

Never attach real account identifiers, tokens, orders, holdings, QMT logs, Quantclass data, configuration files or diagnostic archives that have not been reviewed.

## Scope

In scope:

- unintended process termination or recovery;
- bypass of recovery authorization or operator confirmation;
- credential, account or local-path disclosure;
- unsafe installer/uninstaller behavior;
- release artifact or dependency supply-chain issues.

Out of scope:

- vulnerabilities in QMT, XTQuant, Quantclass or broker software that Quant Guardian does not introduce;
- trading strategy outcomes, data quality or market losses;
- denial of service requiring physical access to an already compromised Windows account.

This policy is not a promise of reward or response time.
