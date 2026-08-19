# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path.cwd()
source_root = project_root / "src"
probe_source_root = source_root / "quant_guardian"
probe_datas = [
    (
        str(path),
        str(Path("probe_runtime") / path.parent.relative_to(source_root)),
    )
    for path in probe_source_root.rglob("*.py")
]

common_hidden_imports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "psutil",
    "qrcode",
]

gui_analysis = Analysis(
    [str(source_root / "quant_guardian" / "__main__.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=probe_datas,
    hiddenimports=common_hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["xtquant"],
    noarchive=False,
    optimize=1,
)
gui_pyz = PYZ(gui_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="Quant Guardian",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

gateway_analysis = Analysis(
    [str(source_root / "quant_guardian" / "gateway" / "__main__.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=[],
    hiddenimports=common_hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["xtquant"],
    noarchive=False,
    optimize=1,
)
gateway_pyz = PYZ(gateway_analysis.pure)
gateway_exe = EXE(
    gateway_pyz,
    gateway_analysis.scripts,
    [],
    exclude_binaries=True,
    name="Quant Guardian Gateway",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    gui_exe,
    gateway_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    gateway_analysis.binaries,
    gateway_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Quant Guardian",
)
