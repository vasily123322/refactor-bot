from __future__ import annotations

from typing import Any


SAFE_DELIVERY_ERROR = "Telegram delivery failed"
UNKNOWN_DELIVERY_ERROR = (
    "scheduler execution lease expired with unknown delivery outcome; "
    "automatic retry disabled"
)
NO_MESSAGE_IDS_ERROR = "no message ids returned"
GENERIC_SCHEDULER_ERROR = "Scheduler task failed"

# Only strings fully controlled by our code may cross the legacy PostTask ->
# Publication/Studio boundary verbatim. Historical arbitrary error text can contain
# Telegram/provider URLs, request payloads, database parameters or credentials.
_PUBLIC_SAFE_ERRORS = frozenset(
    {
        SAFE_DELIVERY_ERROR,
        UNKNOWN_DELIVERY_ERROR,
        NO_MESSAGE_IDS_ERROR,
        # Historical test/legacy builds used this fixed operator-safe message.
        "telegram unavailable",
    }
)


def public_scheduler_error(value: Any) -> str:
    text = str(value or "").strip()
    if text in _PUBLIC_SAFE_ERRORS:
        return text
    return GENERIC_SCHEDULER_ERROR
