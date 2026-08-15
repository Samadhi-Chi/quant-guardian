from __future__ import annotations

import ctypes
import os


class NetworkMonitor:
    def is_available(self) -> bool:
        if os.name != "nt":
            return True
        flags = ctypes.c_ulong()
        try:
            return bool(ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
        except (AttributeError, OSError):
            return True
