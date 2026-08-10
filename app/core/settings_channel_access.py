from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.domain.models import Channel, Client, GrabSource, PostTask


_DIRECT_CHANNEL_PATTERNS = (
    re.compile(r"^channels_settings_(?P<channel_id>\d+)$"),
    re.compile(
        r"^(?:settings_post|post_replace_autosign|post_avtostring|post_split|split_add|split_remove|"
        r"settings_application|app_mode_auto|app_mode_manual|settings_graber|graber_add|graber_remove|"
        r"settings_delete)_(?P<channel_id>\d+)$"
    ),
    re.compile(r"^bot_manage_(?P<channel_id>\d+)$"),
    re.compile(r"^bot_manage_require_dm_preset:(?P<channel_id>\d+)$"),
    re.compile(r"^open_service_menu_[a-z0-9_]+:(?P<channel_id>\d+)$"),
    re.compile(r"^service_toggle_[a-z0-9_]+:[a-z0-9_]+:(?P<channel_id>\d+)$"),
    re.compile(r"^tz_pick:(?P<channel_id>\d+):.+$"),
    re.compile(r"^tz_set_offset:(?P<channel_id>\d+):-?\d+$"),
    # Content-plan callbacks carrying Channel.id directly. These are parsed at the
    # root router so forged callback data cannot reach tenant-specific handlers.
    re.compile(r"^cp_pick_channel_(?P<channel_id>\d+)$"),
    re.compile(
        r"^cp_(?:day_prev|day_next|open_cal|calendar_back|month_prev|month_next|pick_day):"
        r"(?P<channel_id>\d+):\d{4}-\d{2}-\d{2}$"
    ),
)

_GRAB_SOURCE_PATTERNS = (
    (re.compile(r"^graber_src_(?P<claimed_channel_id>\d+)_(?P<source_id>\d+)$"), True),
    (re.compile(r"^graber_del_(?P<source_id>\d+)_(?P<claimed_channel_id>\d+)$"), True),
    (re.compile(r"^graber_flag_(?P<source_id>\d+)_[a-z0-9_]+$"), False),
)

_POST_TASK_PATTERNS = (
    re.compile(
        r"^cp_(?:open_post|edit_post|delete_post):(?P<post_task_id>\d+):"
        r"\d{4}-\d{2}-\d{2}$"
    ),
    # repeat_group_id is currently the legacy root PostTask id. Repeat transport is
    # intentionally not retained yet, so resolving the root row is fail-closed and
    # remains valid until canonical repeat lineage replaces this compatibility seam.
    re.compile(r"^cp_repeat_off:(?P<post_task_id>\d+)$"),
)


def _direct_channel_id_from_callback(data: str | None) -> int | None:
    if not data:
        return None
    for pattern in _DIRECT_CHANNEL_PATTERNS:
        match = pattern.fullmatch(data)
        if match:
            return int(match.group("channel_id"))
    return None


def _grab_source_ref_from_callback(data: str | None) -> tuple[int, int | None] | None:
    if not data:
        return None
    for pattern, has_claimed_channel in _GRAB_SOURCE_PATTERNS:
        match = pattern.fullmatch(data)
        if not match:
            continue
        source_id = int(match.group("source_id"))
        claimed_channel_id = (
            int(match.group("claimed_channel_id")) if has_claimed_channel else None
        )
        return source_id, claimed_channel_id
    return None


def _post_task_id_from_callback(data: str | None) -> int | None:
    if not data:
        return None
    for pattern in _POST_TASK_PATTERNS:
        match = pattern.fullmatch(data)
        if match:
            return int(match.group("post_task_id"))
    return None


async def _resolve_grab_source_target(
    session: AsyncSession, source_ref: tuple[int, int | None]
) -> int | None:
    source_id, claimed_channel_id = source_ref
    source = await session.get(GrabSource, source_id)
    if source is None:
        return None
    target_channel_id = int(source.target_channel_id)
    if claimed_channel_id is not None and claimed_channel_id != target_channel_id:
        return None
    return target_channel_id


async def _resolve_post_task_target(
    session: AsyncSession, post_task_id: int
) -> int | None:
    task = await session.get(PostTask, int(post_task_id))
    if task is None:
        return None
    try:
        channel_id = int(task.channel_id)
    except (TypeError, ValueError, OverflowError):
        return None
    return channel_id if channel_id > 0 else None


async def _resolve_channel_target(
    session: AsyncSession, callback_data: str | None
) -> tuple[bool, int | None]:
    """Return (recognized, channel_id). Recognized + None means deny."""
    source_ref = _grab_source_ref_from_callback(callback_data)
    if source_ref is not None:
        return True, await _resolve_grab_source_target(session, source_ref)

    post_task_id = _post_task_id_from_callback(callback_data)
    if post_task_id is not None:
        return True, await _resolve_post_task_target(session, post_task_id)

    direct_channel_id = _direct_channel_id_from_callback(callback_data)
    if direct_channel_id is not None:
        return True, direct_channel_id
    return False, None


async def _user_owns_channel(
    session: AsyncSession, *, tg_user_id: int, channel_id: int
) -> bool:
    stmt = (
        select(Channel.id)
        .join(Client, Channel.owner_id == Client.id)
        .where(Channel.id == channel_id, Client.tg_user_id == tg_user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


class SettingsChannelOwnerMiddleware(BaseMiddleware):
    """Enforce ownership before callbacks carrying tenant-scoped identities."""

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        callback_data = getattr(event, "data", None)
        source_ref = _grab_source_ref_from_callback(callback_data)
        direct_channel_id = _direct_channel_id_from_callback(callback_data)
        post_task_id = _post_task_id_from_callback(callback_data)

        # This middleware is installed on the root router. Avoid touching the DB
        # for the overwhelming majority of callbacks with no tenant-scoped identity.
        if source_ref is None and direct_channel_id is None and post_task_id is None:
            return await handler(event, data)

        async with AsyncSessionLocal() as session:
            if source_ref is not None:
                channel_id = await _resolve_grab_source_target(session, source_ref)
            elif post_task_id is not None:
                channel_id = await _resolve_post_task_target(session, post_task_id)
            else:
                channel_id = direct_channel_id

            user_id = int(
                getattr(getattr(event, "from_user", None), "id", 0) or 0
            )
            allowed = bool(
                user_id
                and channel_id is not None
                and await _user_owns_channel(
                    session, tg_user_id=user_id, channel_id=channel_id
                )
            )

        if not allowed:
            await event.answer("Нет доступа к этому каналу", show_alert=True)
            return None
        return await handler(event, data)
