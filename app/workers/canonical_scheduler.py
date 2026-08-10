from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel, PostTask
from app.services.telegram_results import normalize_telegram_message_ids
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


@dataclass(frozen=True, slots=True)
class _DelayedDeleteState:
    chat_id: int
    message_ids: tuple[int, ...]
    report: bool
    result_link: str | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _payload_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return dict(value)


def _due_at(post: PostTask, payload: Mapping[str, Any]) -> datetime | None:
    try:
        seconds = int(
            payload.get("autodelete_effective_seconds")
            or payload.get("autodelete_seconds")
            or 0
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if seconds <= 0:
        return None

    raw_due = payload.get("autodelete_at")
    if raw_due is not None:
        if not isinstance(raw_due, str) or not raw_due.strip() or len(raw_due) > 128:
            return None
        text = raw_due.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _as_utc(datetime.fromisoformat(text))
        except (TypeError, ValueError, OverflowError):
            return None

    scheduled = getattr(post, "scheduled_at", None)
    if scheduled is None:
        return None
    return _as_utc(scheduled) + timedelta(seconds=seconds)


class Scheduler(PublicationScheduler):
    """Publication-aware scheduler with stale local autodelete timer protection."""

    async def _delayed_delete_state(
        self,
        session: AsyncSession,
        *,
        post_id: int,
        now: datetime | None = None,
    ) -> _DelayedDeleteState | None:
        post = await session.get(PostTask, int(post_id))
        if post is None or str(post.status) != "done":
            return None
        payload = _payload_mapping(post.payload)
        if payload is None or payload.get("autodeleted") is True:
            return None
        due = _due_at(post, payload)
        current = _as_utc(now or datetime.now(timezone.utc))
        if due is None or due > current:
            return None
        ids = normalize_telegram_message_ids(payload.get("result_ids"))
        if not ids:
            return None
        channel = await session.get(Channel, int(post.channel_id))
        if channel is None:
            return None
        report = payload.get("autodelete_report") is True
        result_link = payload.get("result_link")
        return _DelayedDeleteState(
            chat_id=int(channel.tg_chat_id),
            message_ids=tuple(ids),
            report=report,
            result_link=(str(result_link) if isinstance(result_link, str) else None),
        )

    async def _current_delayed_delete_state(
        self,
        *,
        post_id: int,
    ) -> _DelayedDeleteState | None:
        if self.session_factory is not None:
            async with self.session_factory() as session:
                return await self._delayed_delete_state(session, post_id=post_id)
        if self.session is None:
            return None
        return await self._delayed_delete_state(self.session, post_id=post_id)

    async def _del_later(
        self,
        bot,
        chat_id: int,
        msg_ids: list[int],
        delay: int,
        post_id_val: int,
        report: bool,
        link_val: str | None,
    ) -> None:
        # The original local task captures delivery/timer values at publication time.
        # After an edit those values may be stale, so sleep first and then reload the
        # authoritative compatibility row before any Telegram side effect.
        await asyncio.sleep(max(0, int(delay)))
        state = await self._current_delayed_delete_state(post_id=int(post_id_val))
        if state is None:
            return
        await super()._del_later(
            bot,
            state.chat_id,
            list(state.message_ids),
            0,
            int(post_id_val),
            state.report,
            state.result_link,
        )
