from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "account",
    "account_id",
    "accountid",
    "asset",
    "balance",
    "cash",
    "password",
    "secret",
    "token",
    "webhook",
    "authorization",
    "total_asset",
    "market_value",
    "usable",
}

ACCOUNT_PATTERN = re.compile(r"(?<!\d)(\d{8,20})(?!\d)")
TOKEN_PATTERN = re.compile(
    r"""(?ix)
    (
      [\"']?(?:token|secret|password|authorization|webhook)[\"']?
      \s*[:=]\s*
    )
    ([\"']?)
    ([^\"'\s,;}]+)
    ([\"']?)
    """
)
URL_SECRET_PATTERN = re.compile(r"(?i)([?&](?:key|token|secret)=)[^&\s]+")


def mask_identifier(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return "*" * max(4, len(value) - 2) + value[-2:]


def redact_text(value: str) -> str:
    value = ACCOUNT_PATTERN.sub(lambda match: mask_identifier(match.group(1)), value)
    value = TOKEN_PATTERN.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}<redacted>"
            f"{match.group(4) if match.group(4) == match.group(2) else ''}"
        ),
        value,
    )
    value = URL_SECRET_PATTERN.sub(lambda match: f"{match.group(1)}<redacted>", value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(item in normalized for item in SENSITIVE_KEYS)


def redact(value: Any, *, key: str = "") -> Any:
    if key and _is_sensitive_key(key):
        if isinstance(value, str) and "account" in key.casefold():
            return mask_identifier(value)
        return "<redacted>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(item_key): redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def _common_windows_root(paths: list[str]) -> str:
    cleaned = [ntpath.normpath(path) for path in paths if path and ntpath.isabs(path)]
    if not cleaned:
        return ""
    try:
        common = ntpath.commonpath(cleaned)
    except ValueError:
        return ""
    drive, tail = ntpath.splitdrive(common)
    if not drive or tail in {"", "\\"}:
        return ""
    return common


@dataclass(frozen=True, slots=True)
class PathRedactor:
    replacements: tuple[tuple[str, str], ...]

    @classmethod
    def from_config(cls, config: Any) -> PathRedactor:
        qmt_paths = [
            ntpath.dirname(str(config.qmt.launcher)),
            str(config.qmt.working_directory),
            str(config.qmt.userdata_directory),
            str(config.qmt.log_directory),
        ]
        quantclass_paths = [
            ntpath.dirname(str(config.trade_system.client_executable)),
            ntpath.dirname(str(config.trade_system.quantclass_config)),
            str(config.trade_system.data_root),
        ]
        candidates: list[tuple[str, str]] = []
        qmt_root = _common_windows_root(qmt_paths)
        if qmt_root:
            candidates.append((qmt_root, "<QMT_ROOT>"))
        quantclass_root = _common_windows_root(quantclass_paths)
        if quantclass_root:
            candidates.append((quantclass_root, "<QUANTCLASS_ROOT>"))
        deprecated_rocket_log = str(config.rocket.log_directory)
        if deprecated_rocket_log and ntpath.isabs(deprecated_rocket_log):
            candidates.append((deprecated_rocket_log, "<QUANTCLASS_LOG_ROOT>"))

        environment_paths = (
            (os.environ.get("TEMP", ""), "%TEMP%"),
            (os.environ.get("TMP", ""), "%TEMP%"),
            (os.environ.get("LOCALAPPDATA", ""), "%LOCALAPPDATA%"),
            (os.environ.get("APPDATA", ""), "%APPDATA%"),
            (os.environ.get("USERPROFILE", ""), "%USERPROFILE%"),
            (str(Path.home()), "%USERPROFILE%"),
        )
        candidates.extend(environment_paths)

        unique: dict[str, tuple[str, str]] = {}
        for raw, placeholder in candidates:
            if not raw:
                continue
            normalized = ntpath.normpath(str(raw)).rstrip("\\/")
            if not normalized:
                continue
            unique.setdefault(normalized.casefold(), (normalized, placeholder))
        ordered = sorted(unique.values(), key=lambda item: len(item[0]), reverse=True)
        return cls(tuple(ordered))

    def redact_text(self, value: str) -> str:
        sanitized = value
        for path, placeholder in self.replacements:
            variants = {
                path,
                path.replace("\\", "/"),
                path.replace("\\", "\\\\"),
            }
            for variant in sorted(variants, key=len, reverse=True):
                sanitized = re.sub(
                    re.escape(variant),
                    lambda _match, replacement=placeholder: replacement,
                    sanitized,
                    flags=re.IGNORECASE,
                )
        return sanitized

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {
                str(item_key): self.redact(item_value)
                for item_key, item_value in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [self.redact(item) for item in value]
        return value
