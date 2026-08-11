from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.models import Channel, PostTask
from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import compute_next_repeat_time
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
    """Publication-aware scheduler with guarded canonical migration shadows."""

    def __init__(
        self,
        *args,
        repeat_shadow_planning: bool | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._repeat_shadow_planning = (
            bool(settings.canonical_repeat_shadow_planning_enabled)
            if repeat_shadow_planning is None
            else bool(repeat_shadow_planning)
        )

    @staticmethod
    def _shadow_repeat_expected_at(post: PostTask, payload: Mapping[str, Any]) -> datetime | None:
        if not bool(payload.get("repeat_on", False)):
            return None
        try:
            repeat_seconds = int(payload.get("repeat_seconds") or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if repeat_seconds <= 0:
            return None
        now = datetime.now(timezone.utc)
        scheduled = getattr(post, "scheduled_at", None)
        base = _as_utc(scheduled) if scheduled is not None else now
        first_candidate = base + timedelta(seconds=repeat_seconds)
        return compute_next_repeat_time(first_candidate, repeat_seconds, now)

    async def _shadow_repeat_reserve(
        self,
        session: AsyncSession,
        *,
        post: PostTask,
        expected_at: datetime,
    ) -> int | None:
        async def reserve_in(shadow_session: AsyncSession, shadow_post: PostTask) -> int | None:
            bridge = LegacyPublicationBridge(shadow_session)
            publication = await bridge.reconcile_task(shadow_post)
            if publication is None:
                publication = await mirror_legacy_post_task(shadow_session, shadow_post)
            if publication is None:
                return None
            result = await CanonicalRepeatPlanReservationService(
                shadow_session
            ).reserve_next(
                int(publication.id),
                after=_as_utc(expected_at) - timedelta(microseconds=1),
            )
            if result.outcome in {"reserved", "already_reserved"}:
                logger.debug(
                    "Scheduler: canonical repeat shadow reserved source_publication_id={} "
                    "post_id={} outcome={} scheduled_at={}",
                    int(publication.id),
                    int(shadow_post.id),
                    result.outcome,
                    _as_utc(expected_at).isoformat(),
                )
                return int(publication.id)
            if result.outcome == "conflict":
                logger.warning(
                    "Scheduler: canonical repeat shadow reservation conflict post_id={}",
                    int(shadow_post.id),
                )
            else:
                logger.debug(
                    "Scheduler: canonical repeat shadow reservation skipped post_id={} "
                    "outcome={}",
                    int(shadow_post.id),
                    result.outcome,
                )
            return None

        try:
            if self.session_factory is not None:
                async with self.session_factory() as shadow_session:
                    shadow_post = await shadow_session.get(PostTask, int(post.id))
                    if shadow_post is None:
                        return None
                    return await reserve_in(shadow_session, shadow_post)
            return await reserve_in(session, post)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Scheduler: canonical repeat shadow reservation failed post_id={} type={}",
                int(post.id),
                type(exc).__name__,
            )
            return None

    async def _shadow_repeat_verify(
        self,
        session: AsyncSession,
        *,
        source_publication_id: int,
        post_id: int,
    ) -> None:
        async def verify_in(shadow_session: AsyncSession) -> None:
            result = await CanonicalRepeatReservationVerifier(shadow_session).verify(
                int(source_publication_id)
            )
            if result.outcome == "matched":
                logger.debug(
                    "Scheduler: canonical repeat shadow matched source_publication_id={} "
                    "successor_publication_id={} successor_post_id={}",
                    int(source_publication_id),
                    result.successor_publication_id,
                    result.successor_legacy_post_task_id,
                )
                return
            logger.warning(
                "Scheduler: canonical repeat shadow mismatch source_publication_id={} "
                "post_id={} outcome={}",
                int(source_publication_id),
                int(post_id),
                result.outcome,
            )

        try:
            if self.session_factory is not None:
                async with self.session_factory() as shadow_session:
                    await verify_in(shadow_session)
            else:
                await verify_in(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Scheduler: canonical repeat shadow verification failed post_id={} type={}",
                int(post_id),
                type(exc).__name__,
            )

    async def _schedule_next_repeat_if_needed(
        self,
        session: AsyncSession,
        post: PostTask,
        pl: dict,
    ) -> None:
        if not self._repeat_shadow_planning:
            await super()._schedule_next_repeat_if_needed(session, post, pl)
            return

        expected_at = self._shadow_repeat_expected_at(post, pl)
        source_publication_id = None
        if expected_at is not None:
            source_publication_id = await self._shadow_repeat_reserve(
                session,
                post=post,
                expected_at=expected_at,
            )

        # Legacy PostTask scheduling remains authoritative in shadow mode.
        await super()._schedule_next_repeat_if_needed(session, post, pl)

        if source_publication_id is not None:
            await self._shadow_repeat_verify(
                session,
                source_publication_id=source_publication_id,
                post_id=int(post.id),
            )

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
