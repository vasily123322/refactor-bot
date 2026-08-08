from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.domain.models import Channel, Client


_CHANNEL_CALLBACK_PATTERNS = (
    # Callbacks where the channel id is followed by another numeric/string option.
    re.compile(r"^ai_set_hashtags_count_(?P<channel_id>\d+)_\d+$"),
    re.compile(
        r"^ai_source_(?:item|nop|toggle|cite|mode|delete)_(?P<channel_id>\d+)_\d+$"
    ),
    re.compile(r"^ai_priority_set_(?P<channel_id>\d+)_[a-z0-9_-]+$"),
    # Single-channel callbacks.
    re.compile(r"^ai_toggle_[a-z0-9_]+_(?P<channel_id>\d+)$"),
    re.compile(r"^ai_forbidden_[a-z0-9_]+_(?P<channel_id>\d+)$"),
    re.compile(r"^ai_hashtags_count_(?P<channel_id>\d+)$"),
    re.compile(
        r"^ai_source_(?:add|list|digest|drafts)_(?P<channel_id>\d+)$"
    ),
    re.compile(r"^ai_priority_(?P<channel_id>\d+)$"),
    re.compile(r"^ai_text_history_(?P<channel_id>\d+)$"),
    re.compile(r"^neu_[a-z0-9_]+_(?P<channel_id>\d+)$"),
    re.compile(r"^settings_neuropost_(?P<channel_id>\d+)$"),
)


def _channel_id_from_callback(data: str | None) -> int | None:
    if not data:
        return None
    for pattern in _CHANNEL_CALLBACK_PATTERNS:
        match = pattern.fullmatch(data)
        if match:
            return int(match.group("channel_id"))
    return None


class ChannelOwnerMiddleware(BaseMiddleware):
    """Block protected channel callbacks when the caller is not the owner."""

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        channel_id = _channel_id_from_callback(getattr(event, "data", None))
        if channel_id is None:
            return await handler(event, data)

        user_id = int(getattr(getattr(event, "from_user", None), "id", 0) or 0)
        if not user_id:
            await event.answer("Нет доступа", show_alert=True)
            return None

        async with AsyncSessionLocal() as session:
            stmt = (
                select(Channel.id)
                .join(Client, Channel.owner_id == Client.id)
                .where(Channel.id == channel_id, Client.tg_user_id == user_id)
            )
            allowed = (await session.execute(stmt)).scalar_one_or_none() is not None

        if not allowed:
            await event.answer("Нет доступа к этому каналу", show_alert=True)
            return None
        return await handler(event, data)
