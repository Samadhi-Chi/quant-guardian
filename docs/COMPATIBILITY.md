# Compatibility

## Tested baseline

| Component | Tested version | Status |
|---|---:|---|
| Windows | Windows 10 / 11 x64 | Supported |
| QMT | 2.0.23.0 | Tested baseline |
| Quantclass Client | 4.1.1 | Tested for monitoring |
| XTQuant | 250807.1.2 | Tested in isolated Python 3.11 worker |
| Python source runtime | 3.11–3.14 x64 | Supported range |
| PySide6 | 6.11.1 | Release baseline |

Versions not listed above are unverified, not necessarily incompatible. Broker-customized QMT builds may change launcher paths, login behavior or confirmation dialogs.

## Integration ownership

- QMT, XTQuant and Quantclass are supplied and licensed by their respective owners.
- Quant Guardian does not bundle their binaries or source.
- Quant Guardian observes public/local interfaces and configured files; it does not patch those products.
- QMT/Quantclass support requests should be directed to their vendors when the issue reproduces without Quant Guardian.

## Python split

The desktop application supports Python 3.11–3.14. The Release is built with Python 3.14.6. XTQuant is deliberately isolated in a compatible Python 3.11 child process so its native ABI does not constrain the UI runtime.

Python 3.11.9 is the final 3.11 release with official Windows installers. Its use is limited to the isolated local probe; update XTQuant and the probe runtime when the vendor publishes a newer supported ABI.
