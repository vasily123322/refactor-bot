from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import Channel, PostTask


_LEDGER_KEY = "_legacy_time_views_delete_action_ledger_v1"

ReserveOutcome = Literal[
    "reserved",
    "not_applicable",
    "already_reserved",
    "terminal",
    "unknown",
    "invalid_target",
]


@dataclass(frozen=True)
class LegacyTimeViewsDeleteReservation:
    post_task_id: int
    chat_id: int
    message_ids: tuple[int, ...]
    token: str


@dataclass(frozen=True)
class LegacyTimeViewsDeleteReserveResult:
    outcome: ReserveOutcome
    reservation: LegacyTimeViewsDeleteReservation | None = None


class LegacyTimeViewsDeleteActionLedger:
    """One-way destructive reservation for legacy mixed time+views deletion.

    A committed ``reserved`` entry is itself a durable no-replay barrier.  Only
    the call that creates that entry receives the opaque reservation token and
    is therefore allowed to call the provider.  ``reserved``/``unknown`` are
    intentionally never reclaimed automatically after crashes or ambiguous
    provider outcomes.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalized_ids(value) -> tuple[int, ...] | None:
        if not isinstance(value, (list, tuple)) or not value:
            return None
        try:
            return tuple(int(item) for item in value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_legacy_mixed(payload: dict) -> bool:
        try:
            seconds = int(
                payload.get("autodelete_effective_seconds")
                or payload.get("autodelete_seconds")
                or 0
            )
            views = int(payload.get("autodelete_views") or 0)
        except (TypeError, ValueError):
            return False
        return seconds > 0 and views > 0

    @staticmethod
    def _reservation_from_entry(entry: dict) -> LegacyTimeViewsDeleteReservation | None:
        target = entry.get("target")
        if not isinstance(target, dict):
            return None
        ids = LegacyTimeViewsDeleteActionLedger._normalized_ids(
            target.get("message_ids")
        )
        token = entry.get("token")
        try:
            post_task_id = int(entry.get("post_task_id"))
            chat_id = int(target.get("chat_id"))
        except (TypeError, ValueError):
            return None
        if not ids or not isinstance(token, str) or not token:
            return None
        return LegacyTimeViewsDeleteReservation(
            post_task_id=post_task_id,
            chat_id=chat_id,
            message_ids=ids,
            token=token,
        )

    async def reserve(
        self,
        *,
        post_task_id: int,
        chat_id: int,
        message_ids: list[int] | tuple[int, ...],
    ) -> LegacyTimeViewsDeleteReserveResult:
        requested_ids = self._normalized_ids(message_ids)
        if requested_ids is None:
            return LegacyTimeViewsDeleteReserveResult("invalid_target")

        async with self._session_factory() as session:
            row = await session.execute(
                select(PostTask)
                .where(PostTask.id == int(post_task_id))
                .with_for_update()
            )
            post = row.scalar_one_or_none()
            if post is None:
                await session.rollback()
                return LegacyTimeViewsDeleteReserveResult("invalid_target")

            payload = dict(post.payload or {})
            existing = payload.get(_LEDGER_KEY)
            if existing is not None:
                if not isinstance(existing, dict):
                    await session.rollback()
                    return LegacyTimeViewsDeleteReserveResult("unknown")
                try:
                    owner_id = int(existing.get("post_task_id"))
                except (TypeError, ValueError):
                    owner_id = None
                if owner_id == int(post.id):
                    state = str(existing.get("state") or "unknown")
                    await session.rollback()
                    if state == "reserved":
                        return LegacyTimeViewsDeleteReserveResult("already_reserved")
                    if state == "succeeded":
                        return LegacyTimeViewsDeleteReserveResult("terminal")
                    return LegacyTimeViewsDeleteReserveResult("unknown")
                # Repeat payloads can inherit runtime fields.  A ledger owned by
                # another PostTask is not authority for this task and is replaced
                # only when this task successfully creates its own reservation.

            if not self._is_legacy_mixed(payload):
                await session.rollback()
                return LegacyTimeViewsDeleteReserveResult("not_applicable")

            persisted_ids = self._normalized_ids(payload.get("result_ids"))
            if persisted_ids is None or persisted_ids != requested_ids:
                await session.rollback()
                return LegacyTimeViewsDeleteReserveResult("invalid_target")

            channel = await session.get(Channel, int(post.channel_id))
            try:
                persisted_chat_id = int(channel.tg_chat_id) if channel is not None else None
            except (TypeError, ValueError):
                persisted_chat_id = None
            if persisted_chat_id != int(chat_id):
                await session.rollback()
                return LegacyTimeViewsDeleteReserveResult("invalid_target")

            token = str(uuid4())
            reservation = LegacyTimeViewsDeleteReservation(
                post_task_id=int(post.id),
                chat_id=int(chat_id),
                message_ids=requested_ids,
                token=token,
            )
            payload[_LEDGER_KEY] = {
                "post_task_id": reservation.post_task_id,
                "target": {
                    "chat_id": reservation.chat_id,
                    "message_ids": list(reservation.message_ids),
                },
                "token": reservation.token,
                "state": "reserved",
                "reserved_at": self._now_iso(),
            }
            post.payload = payload
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        # Returning this token is the authorization boundary: it happens only
        # after the reservation commit completed successfully.
        return LegacyTimeViewsDeleteReserveResult(
            "reserved", reservation=reservation
        )

    async def mark_succeeded(
        self, reservation: LegacyTimeViewsDeleteReservation
    ) -> bool:
        return await self._finalize(reservation, state="succeeded")

    async def mark_unknown(
        self, reservation: LegacyTimeViewsDeleteReservation
    ) -> bool:
        return await self._finalize(reservation, state="unknown")

    async def _finalize(
        self,
        reservation: LegacyTimeViewsDeleteReservation,
        *,
        state: Literal["succeeded", "unknown"],
    ) -> bool:
        async with self._session_factory() as session:
            row = await session.execute(
                select(PostTask)
                .where(PostTask.id == int(reservation.post_task_id))
                .with_for_update()
            )
            post = row.scalar_one_or_none()
            if post is None:
                await session.rollback()
                return False

            payload = dict(post.payload or {})
            entry = payload.get(_LEDGER_KEY)
            if not isinstance(entry, dict) or entry.get("state") != "reserved":
                await session.rollback()
                return False

            current = self._reservation_from_entry(entry)
            if current != reservation:
                await session.rollback()
                return False

            persisted_ids = self._normalized_ids(payload.get("result_ids"))
            if persisted_ids != reservation.message_ids:
                await session.rollback()
                return False

            channel = await session.get(Channel, int(post.channel_id))
            try:
                persisted_chat_id = int(channel.tg_chat_id) if channel is not None else None
            except (TypeError, ValueError):
                persisted_chat_id = None
            if persisted_chat_id != reservation.chat_id:
                await session.rollback()
                return False

            updated = dict(entry)
            updated["state"] = state
            updated["finalized_at"] = self._now_iso()
            payload[_LEDGER_KEY] = updated
            post.payload = payload
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            return True
