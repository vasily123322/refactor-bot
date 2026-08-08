from __future__ import annotations

import re
from contextlib import suppress
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.domain.models import Channel, Client


_CHANNEL_CALLBACK_PATTERNS = (
    # Callbacks where the channel id is followed by another numeric/string option.
    re.compile(r"^ai_set_hashtags_count_(?P<channel_id>\d+)_\d+$"),
    re.compile(
        r"^ai_source_(?:item|nop|toggle|cite|mode|delete)_(?P<channel_id>\d+)_\d+$"
    ),
    re.compile(r"^source_draft_(?:page|open)_(?P<channel_id>\d+)_\d+$"),
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


async def user_owns_channel(*, user_id: int, channel_id: int) -> bool:
    """Return whether a Telegram user owns the internal channel id."""
    if user_id <= 0 or channel_id <= 0:
        return False

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Channel.id)
            .join(Client, Channel.owner_id == Client.id)
            .where(Channel.id == channel_id, Client.tg_user_id == user_id)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None


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
        if not await user_owns_channel(user_id=user_id, channel_id=channel_id):
            await event.answer("Нет доступа к этому каналу", show_alert=True)
            return None
        return await handler(event, data)


class ChannelOwnerStateMiddleware(BaseMiddleware):
    """Re-check ownership before handlers consume a channel id stored in FSM state."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if state is None:
            return await handler(event, data)

        state_data = await state.get_data()
        raw_channel_id = state_data.get("channel_id")
        if raw_channel_id is None:
            return await handler(event, data)

        try:
            channel_id = int(raw_channel_id)
        except (TypeError, ValueError):
            channel_id = 0

        user_id = int(getattr(getattr(event, "from_user", None), "id", 0) or 0)
        if await user_owns_channel(user_id=user_id, channel_id=channel_id):
            return await handler(event, data)

        with suppress(Exception):
            await state.clear()
        with suppress(Exception):
            await event.answer("Нет доступа к этому каналу. Действие отменено.")
        return None
