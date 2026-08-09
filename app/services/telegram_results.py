from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def normalize_telegram_message_ids(value: Any) -> list[int]:
    """Return validated positive Telegram message IDs or an empty fail-closed result.

    Scheduler-owned payloads write the full result list atomically. If any legacy item
    is malformed, treat the whole transport list as untrusted instead of projecting a
    misleading partial delivery result.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return []

    result: list[int] = []
    for item in value:
        if isinstance(item, bool):
            return []
        try:
            message_id = int(item)
        except (TypeError, ValueError, OverflowError):
            return []
        if message_id <= 0:
            return []
        result.append(message_id)
    return result


def normalize_telegram_result_link(value: Any) -> str | None:
    """Allow only canonical HTTPS t.me post links produced by the scheduler."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 2048:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None

    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() != "https":
        return None
    if (parsed.hostname or "").lower() != "t.me":
        return None
    if parsed.username is not None or parsed.password is not None or port is not None:
        return None
    if parsed.query or parsed.fragment:
        return None
    if not parsed.path or parsed.path == "/" or not parsed.path.startswith("/"):
        return None
    return f"https://t.me{parsed.path}"
