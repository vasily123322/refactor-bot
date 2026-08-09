from __future__ import annotations

from typing import Any


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
