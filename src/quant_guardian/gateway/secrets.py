from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable
from pathlib import Path

from quant_guardian.gateway.config import gateway_secret_path
from quant_guardian.security.dpapi import protect_text, unprotect_text


class CredentialVault:
    """Small DPAPI-backed credential store scoped to the current Windows user."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        protect: Callable[[str], str] = protect_text,
        unprotect: Callable[[str], str] = unprotect_text,
    ) -> None:
        self.path = path or gateway_secret_path()
        self._protect = protect
        self._unprotect = unprotect
        self._lock = threading.RLock()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or int(raw.get("schema_version", 0)) != 1:
            raise ValueError("invalid messaging credential vault")
        values = raw.get("protected_values", {})
        if not isinstance(values, dict):
            raise ValueError("invalid protected_values in credential vault")
        return {str(key): str(value) for key, value in values.items()}

    def _write(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"schema_version": 1, "protected_values": values},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def set(self, name: str, value: str) -> None:
        key = str(name).strip()
        if not key:
            raise ValueError("credential name cannot be blank")
        with self._lock:
            values = self._read()
            if value:
                values[key] = self._protect(value)
            else:
                values.pop(key, None)
            self._write(values)

    def get(self, name: str, default: str = "") -> str:
        with self._lock:
            protected = self._read().get(str(name), "")
        if not protected:
            return default
        return self._unprotect(protected)

    def has(self, name: str) -> bool:
        with self._lock:
            return bool(self._read().get(str(name)))

    def delete(self, name: str) -> None:
        self.set(name, "")

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._read()))

    def ipc_auth_key(self) -> bytes:
        value = self.get("ipc_auth_key")
        if not value:
            value = secrets.token_urlsafe(48)
            self.set("ipc_auth_key", value)
        return value.encode("utf-8")
