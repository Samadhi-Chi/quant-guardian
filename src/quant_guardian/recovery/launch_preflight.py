"""Read launcher metadata before touching an existing trading process."""
from __future__ import annotations

import ctypes
import os
import re
from functools import lru_cache
from pathlib import Path


def is_elevated() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


@lru_cache(maxsize=16)
def _manifest_level(path: str, modified: int, size: int) -> str:
    del modified, size  # Cache invalidates when the executable changes.
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel.FindResourceW.restype = wintypes.HANDLE
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.LoadResource.restype = wintypes.HANDLE
    kernel.LockResource.argtypes = [wintypes.HANDLE]
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    # Data/image-resource flags never execute third-party entry points.
    module = kernel.LoadLibraryExW(path, None, 0x22)
    if not module:
        return ""
    try:
        for resource_id in (1, 2, 3):
            resource = kernel.FindResourceW(module, resource_id, 24)
            if not resource:
                continue
            length = kernel.SizeofResource(module, resource)
            if not 0 < length <= 1024 * 1024:
                continue
            address = kernel.LockResource(kernel.LoadResource(module, resource))
            if not address:
                continue
            manifest = ctypes.string_at(address, length).replace(b"\x00", b"")
            match = re.search(
                rb"<requestedExecutionLevel\b[^>]*\blevel\s*=\s*[\"']([^\"']+)",
                manifest, re.IGNORECASE,
            )
            if match:
                return match.group(1).decode("ascii", errors="ignore")
    finally:
        kernel.FreeLibrary(module)
    return ""


def launch_permission_problem(launcher: str) -> str:
    if os.name != "nt" or is_elevated():
        return ""
    try:
        path = Path(launcher)
        stat = path.stat()
        level = _manifest_level(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except (OSError, AttributeError, ValueError):
        return ""
    if level.casefold() == "requireadministrator":
        return "QMT启动器要求管理员权限，Guardian当前为普通权限；请以管理员身份运行Guardian，当前QMT不会被关闭"
    return ""
