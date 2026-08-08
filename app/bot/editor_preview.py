from __future__ import annotations

from contextlib import suppress
from typing import Any


async def delete_tracked_preview_messages(
    bot: Any,
    *,
    chat_id: int,
    state_data: dict[str, Any],
) -> None:
    """Best-effort cleanup for all messages tracked by the post editor preview."""
    message_ids: set[int] = set()
    for key in ("preview_msg_id", "preview_text_id", "preview_media_id"):
        value = state_data.get(key)
        if value:
            with suppress(TypeError, ValueError):
                message_ids.add(int(value))
    for value in state_data.get("preview_album_ids") or []:
        with suppress(TypeError, ValueError):
            message_ids.add(int(value))

    for message_id in message_ids:
        with suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
