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

a = Analysis(
    [str(source_root / "quant_guardian" / "__main__.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=probe_datas,
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "psutil",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["xtquant"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Quant Guardian",
)