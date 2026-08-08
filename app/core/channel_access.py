from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.domain.models import Channel, Client


_PROTECTED_PREFIXES = (
    "ai_toggle_",
    "ai_forbidden_",
    "neu_moder_",
)


def _channel_id_from_callback(data: str | None) -> int | None:
    if not data or not data.startswith(_PROTECTED_PREFIXES):
        return None
    try:
        return int(data.rsplit("_", 1)[1])
    except (IndexError, TypeError, ValueError):
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
