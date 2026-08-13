from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal
from app.services.canonical_linked_control_probe import CanonicalLinkedControlProbe


class CanonicalMigrationControlMiddleware(BaseMiddleware):
    """Fail closed for legacy-only controls that would drift canonical authority.

    The old content-plan autodelete editor persists its FSM payload only into PostTask and
    even copies generated `autodelete_effective_seconds`. A linked PostTask is not an
    independent control plane after canonical retirement. Until a dedicated versioned
    canonical runtime-intent editor exists, block entry into that legacy editor for linked
    rows while leaving genuine unlinked legacy fallback unchanged.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        if getattr(event, "data", None) != CB.EDIT_AUTODEL:
            return await handler(event, data)

        state = data.get("state")
        if state is None or not hasattr(state, "get_data"):
            return await handler(event, data)
        try:
            state_data = await state.get_data()
            notice = state_data.get("return_to_notice") or {}
            post_task_id = int(notice.get("post_id") or 0)
        except (TypeError, ValueError, OverflowError, AttributeError):
            post_task_id = 0
        if post_task_id <= 0:
            return await handler(event, data)

        async with self.session_factory() as session:
            publication_id = await CanonicalLinkedControlProbe(
                session
            ).publication_id_for_task(post_task_id)
        if publication_id is None:
            return await handler(event, data)

        await event.answer(
            "Изменение автоудаления для этой публикации временно недоступно: "
            "legacy PostTask больше не является единственным authority. "
            "Автоматических изменений не выполнено.",
            show_alert=True,
        )
        return None
