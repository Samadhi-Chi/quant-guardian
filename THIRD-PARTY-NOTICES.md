# Third-party notices

Quant Guardian 自有代码采用 Apache-2.0。下列组件继续受各自许可证约束。发行 ZIP 中的 licenses/ 目录包含相应许可原文和 Qt for Python 第三方声明。

## Runtime components

| Component | Release baseline | License | Source |
|---|---:|---|---|
| Python | 3.14.6 | PSF License Agreement | <https://www.python.org/> |
| PySide6 / PySide6 Essentials / PySide6 Addons / Shiboken6 | 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only, or commercial | <https://doc.qt.io/qtforpython-6/> |
| Qt libraries distributed with PySide6 | 6.11.1 | Primarily LGPL-3.0/GPL; individual modules and bundled third-party code may differ | <https://doc.qt.io/qtforpython-6/licenses.html> |
| psutil | 7.2.2 | BSD-3-Clause | <https://github.com/giampaolo/psutil> |
| tzdata | 2026.3 | Apache-2.0 | <https://github.com/python/tzdata> |

The Windows one-folder build dynamically loads the Qt DLLs as separate files. Users may replace or relink LGPL-covered Qt components for debugging or modification. Quant Guardian imposes no additional restriction on reverse engineering those LGPL-covered components for that purpose.

## Build and packaging components

| Component | Release baseline | License | Source |
|---|---:|---|---|
| PyInstaller | 6.22.0 | GPL-2.0-or-later with the PyInstaller bootloader exception | <https://pyinstaller.org/> |
| pefile | 2024.8.26 | MIT | <https://github.com/erocarrera/pefile> |
| altgraph | 0.17.5 | MIT | <https://github.com/ronaldoussoren/altgraph> |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | <https://github.com/enthought/pywin32-ctypes> |

Build tools do not change the Apache-2.0 license of Quant Guardian's own source. The exact release dependency inventory is also published as a CycloneDX SBOM.

## External integrations not distributed

Quant Guardian interoperates with QMT, XTQuant and Quantclass installations supplied separately by the user. No QMT, XTQuant, Quantclass Client Pro, Fuel, Aqua, Zeus or Rocket binary or source is included in this repository or Release.

Quantclass Client Pro is source-available under BUSL-1.1 and explicitly states that it is not currently Open Source. Its published change date is 2028-08-22 and its current Additional Use Grant is “None.” See the [upstream license](https://github.com/qtcls/quantclass-client-pro/blob/main/LICENSE). Apache-2.0 for Quant Guardian does not grant any rights to Quantclass Client Pro or other external software.

All product names, company names and trademarks are the property of their respective owners.
