from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])\d{5,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")
_SK_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])", re.IGNORECASE)
_GOOGLE_API_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{30,}(?![A-Za-z0-9_-])")
_BEARER_RE = re.compile(r"(?i)(\bAuthorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_LABELED_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_ -]?key|token|password|secret)\b\s*[:=]\s*)([^\s<>'\"&]{8,})"
)
_URL_CREDENTIAL_RE = re.compile(r"(?P<prefix>https?://[^\s/:@]+:)(?P<password>[^\s/@]+)(?P<suffix>@)", re.IGNORECASE)


def redact_secret_text(value: str) -> str:
    """Remove common credential shapes from text before logs/messages leave the process."""
    text = str(value)
    text = _TELEGRAM_BOT_TOKEN_RE.sub(REDACTED, text)
    text = _SK_TOKEN_RE.sub(REDACTED, text)
    text = _GOOGLE_API_KEY_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _LABELED_SECRET_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _URL_CREDENTIAL_RE.sub(
        lambda m: f"{m.group('prefix')}{REDACTED}{m.group('suffix')}", text
    )
    return text


def redact_value(value: Any) -> Any:
    """Best-effort recursive redaction for structured log extras."""
    if isinstance(value, str):
        return redact_secret_text(value)
    if isinstance(value, Mapping):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def redact_log_record(record: dict[str, Any]) -> bool:
    record["message"] = redact_secret_text(str(record.get("message", "")))
    extra = record.get("extra")
    if isinstance(extra, dict):
        for key, value in list(extra.items()):
            extra[key] = redact_value(value)
    return True
