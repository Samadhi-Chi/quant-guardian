from __future__ import annotations

import base64
import ctypes
import types
import unittest
from unittest.mock import patch

from quant_guardian.security import dpapi


class FakeKernel32:
    def __init__(self) -> None:
        self.freed = []

    def LocalFree(self, pointer) -> None:
        self.freed.append(pointer)


class FakeCrypt32:
    def __init__(self, protected: bytes = b"protected", plain: bytes = b"plain") -> None:
        self.protected = protected
        self.plain = plain
        self.buffers = []
        self.fail_protect = False
        self.fail_unprotect = False

    def _write(self, target_pointer, value: bytes) -> None:
        buffer = ctypes.create_string_buffer(value)
        self.buffers.append(buffer)
        target = target_pointer._obj
        target.cbData = len(value)
        target.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))

    def CryptProtectData(self, *_args):
        if self.fail_protect:
            return False
        self._write(_args[-1], self.protected)
        return True

    def CryptUnprotectData(self, *_args):
        if self.fail_unprotect:
            return False
        self._write(_args[-1], self.plain)
        return True


class DpapiTests(unittest.TestCase):
    def test_blob_keeps_input_bytes_available(self) -> None:
        blob, buffer = dpapi._blob(b"abc")
        self.assertEqual(blob.cbData, 3)
        self.assertEqual(ctypes.string_at(blob.pbData, blob.cbData), b"abc")
        self.assertIsNotNone(buffer)

    def test_protect_and_unprotect_use_dpapi_and_free_output(self) -> None:
        crypt32 = FakeCrypt32(protected=b"cipher", plain="秘密".encode())
        kernel32 = FakeKernel32()
        windll = types.SimpleNamespace(crypt32=crypt32, kernel32=kernel32)
        with patch.object(dpapi.ctypes, "windll", windll):
            protected = dpapi.protect_text("secret")
            plain = dpapi.unprotect_text(base64.b64encode(b"cipher").decode())
        self.assertEqual(protected, base64.b64encode(b"cipher").decode())
        self.assertEqual(plain, "秘密")
        self.assertEqual(len(kernel32.freed), 2)

    def test_dpapi_failure_raises_and_does_not_free_unallocated_output(self) -> None:
        crypt32 = FakeCrypt32()
        kernel32 = FakeKernel32()
        windll = types.SimpleNamespace(crypt32=crypt32, kernel32=kernel32)
        crypt32.fail_protect = True
        with patch.object(dpapi.ctypes, "windll", windll), self.assertRaises(OSError):
            dpapi.protect_text("secret")
        crypt32.fail_protect = False
        crypt32.fail_unprotect = True
        with patch.object(dpapi.ctypes, "windll", windll), self.assertRaises(OSError):
            dpapi.unprotect_text(base64.b64encode(b"cipher").decode())
        self.assertEqual(kernel32.freed, [])

    def test_non_windows_fails_closed(self) -> None:
        with patch.object(dpapi.os, "name", "posix"):
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                dpapi.protect_text("secret")
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                dpapi.unprotect_text("c2VjcmV0")


if __name__ == "__main__":
    unittest.main()
