from __future__ import annotations

import re

from quant_guardian.diagnostics.redaction import redact_text

_LOCAL_PATH = re.compile(
    r"(?i)(?<![\w])(?:file:///)?(?:[a-z]:[\\/]|\\\\)[^\r\n,;，；。\"']+"
)
_TELEGRAM_TOKEN = re.compile(r"(?<![\w])\d{6,12}:[A-Za-z0-9_-]{20,}(?![\w])")


def safe_message_text(value: object, limit: int = 500) -> str:
    text = redact_text(" ".join(str(value or "").split()))
    text = _TELEGRAM_TOKEN.sub("<redacted-token>", text)
    text = _LOCAL_PATH.sub("<LOCAL_PATH>", text)
    return text[: max(0, int(limit))]
