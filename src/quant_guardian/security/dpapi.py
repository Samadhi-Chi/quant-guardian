from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_text(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    source, source_buffer = _blob(value.encode("utf-8"))
    _ = source_buffer
    target = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "Quant Guardian",
        None,
        None,
        None,
        0,
        ctypes.byref(target),
    ):
        raise ctypes.WinError()
    try:
        protected = ctypes.string_at(target.pbData, target.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(target.pbData)


def unprotect_text(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    source, source_buffer = _blob(base64.b64decode(value))
    _ = source_buffer
    target = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(target.pbData)
