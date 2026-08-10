from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


_PUBLIC_POST_PATH = re.compile(r"^/[A-Za-z0-9_]{1,64}/[1-9][0-9]*$")
_PRIVATE_POST_PATH = re.compile(r"^/c/[1-9][0-9]*/[1-9][0-9]*$")


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
    """Allow only exact HTTPS t.me post-link forms produced by this scheduler."""
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
    if not (
        _PUBLIC_POST_PATH.fullmatch(parsed.path)
        or _PRIVATE_POST_PATH.fullmatch(parsed.path)
    ):
        return None
    return f"https://t.me{parsed.path}"


def rewrite_telegram_result_link_message_id(
    value: Any,
    *,
    chat_id: int,
    message_id: int,
) -> str | None:
    """Keep a proven Telegram post-link identity while replacing its message ID.

    Public channel usernames cannot be recovered from a numeric chat id, so an absent
    public link stays absent. Private/supergroup links can be reconstructed from a
    `-100...` chat id even when the historical link is absent.
    """
    try:
        safe_message_id = int(message_id)
        safe_chat_id = int(chat_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if safe_message_id <= 0:
        return None

    normalized = normalize_telegram_result_link(value)
    if normalized is not None:
        parsed = urlsplit(normalized)
        parts = parsed.path.rstrip("/").split("/")
        if parts and parts[-1].isdigit():
            parts[-1] = str(safe_message_id)
            return f"https://t.me{'/'.join(parts)}"

    chat_text = str(safe_chat_id)
    if chat_text.startswith("-100") and len(chat_text) > 4:
        internal = chat_text[4:]
        if internal.isdigit() and int(internal) > 0:
            return f"https://t.me/c/{internal}/{safe_message_id}"
    return None
