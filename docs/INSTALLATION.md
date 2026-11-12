# Installation and removal

## Release ZIP

1. Download the Windows x64 ZIP and SHA256SUMS file from GitHub Releases.
2. Compare the ZIP hash with:

~~~powershell
Get-FileHash .\Quant-Guardian-v0.6.0-beta.1-windows-x64.zip -Algorithm SHA256
~~~

3. Extract it to a normal user-owned directory, not a drive root, user profile root or Programs root.
4. Run Quant Guardian\Quant Guardian.exe in observation mode. The adjacent `Quant Guardian Gateway.exe` is started only after the messaging Gateway is enabled.

The beta is unsigned. SmartScreen may display an unknown-publisher warning. Verify the GitHub repository, asset name and SHA-256 before choosing to run it.

## Optional per-user install

From the extracted release root:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-app.ps1
~~~

The default destination is %LOCALAPPDATA%\Programs\Quant Guardian. Installation:

- stages and hashes the executable;
- reads the generated VERSION file;
- creates .quant-guardian-install.json with the product ID, canonical install root, version and executable SHA-256;
- preserves an existing installation as a timestamped backup.

Custom destinations are allowed only when they are not protected roots.

## Startup and sampling watchdog

After completing configuration, run `scripts/install-task.ps1`. It starts the installed Guardian at sign-in/daily startup and checks it every minute through `--watchdog`. The watchdog only recovers Guardian itself and respects hourly off-session monitoring. It does not enable QMT recovery or control Quantclass. `scripts/uninstall-task.ps1` removes this startup task.

Plan A automatically uses the broker's own Python/XTQuant installation. Do not run the miniQMT Python/XTQuant installer inside a broker installation. Plan X shows process and plan-delivery evidence; actual native strategy execution remains unverified.

Some full-QMT launchers require administrator privileges. Guardian checks this before closing QMT: a non-elevated Guardian refuses the restart and leaves the current QMT running. To authorize elevated Guardian startup, first exit Guardian, then run the following from an administrator PowerShell in the extracted release root:

~~~powershell
.\scripts\install-task.ps1 -RunElevated
Start-ScheduledTask -TaskName "Quant Guardian"
~~~

This explicitly elevates Guardian, not Quantclass or its engines. The ordinary installer retains limited privileges by default. A configured recovery mode is not proof that unattended restart is ready; check the launch-permission status and perform a separately authorized after-hours recovery drill.

## Safe removal

Preview without changing files:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall-app.ps1 -WhatIf
~~~

Remove after the high-impact confirmation:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall-app.ps1
~~~

The uninstaller refuses:

- drive roots, the current user profile, LocalAppData root, Programs root and repository root;
- directories without a valid Quant Guardian install marker;
- marker paths that differ from the requested destination;
- directories without the expected executable;
- installations whose executable hash no longer matches the marker.

There is no force option that bypasses these checks. Configuration, audit logs, the private Python runtime and XTQuant package are intentionally retained under %LOCALAPPDATA%\QuantGuardian.

## Source environment

Use a 64-bit Python 3.11–3.14 installation:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -Dev
~~~

If the XTQuant probe is needed, scripts/install-python-runtime.ps1 installs an isolated Python 3.11.9 runtime after SHA-256 and Authenticode verification. Python 3.11.9 is used because it was the last Python 3.11 release with an official Windows installer; newer 3.11 security releases are source-only.
