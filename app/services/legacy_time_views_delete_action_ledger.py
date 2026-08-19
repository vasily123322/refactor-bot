from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import Channel, Client, PostTask


ReserveOutcome = Literal[
    "reserved",
    "not_applicable",
    "already_reserved",
    "terminal",
    "unknown",
    "invalid_target",
]
DeleteOnceOutcome = Literal[
    "succeeded",
    "not_applicable",
    "already_reserved",
    "terminal",
    "unknown",
    "invalid_target",
    "provider_unknown",
    "finalize_failed",
]

_PERIODIC_SCHEDULER_AUTODELETE_TASK_NAME = "scheduler-autodelete"


@dataclass(frozen=True)
class LegacyTimeViewsDeleteReservation:
    post_task_id: int
    chat_id: int
    message_ids: tuple[int, ...]
    target_fingerprint: str
    token: str


@dataclass(frozen=True)
class LegacyTimeViewsDeleteReserveResult:
    outcome: ReserveOutcome
    reservation: LegacyTimeViewsDeleteReservation | None = None


@dataclass(frozen=True)
class LegacyTimeViewsDeleteOnceResult:
    outcome: DeleteOnceOutcome
    reservation: LegacyTimeViewsDeleteReservation | None = None

    @property
    def handled(self) -> bool:
        return self.outcome != "not_applicable"

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"


class LegacyTimeViewsDeleteActionLedger:
    """Durable at-most-once DELETE authority for legacy mixed time+views tasks.

    The unique ledger row for a PostTask occurrence is the winner election.
    Only the caller whose INSERT commits receives the fresh opaque token and may
    call the provider.  A durable ``reserved`` or ``unknown`` row is a permanent
    automatic no-replay barrier; neither state is reclaimed automatically.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

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
    def _target_fingerprint(
        *, post_task_id: int, chat_id: int, message_ids: tuple[int, ...]
    ) -> str:
        raw = json.dumps(
            {
                "post_task_id": int(post_task_id),
                "chat_id": int(chat_id),
                "message_ids": list(message_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _classify_existing_state(state: str | None) -> ReserveOutcome:
        if state == "reserved":
            return "already_reserved"
        if state == "succeeded":
            return "terminal"
        return "unknown"

    @staticmethod
    def _is_periodic_scheduler_autodelete_task() -> bool:
        task = asyncio.current_task()
        return bool(
            task is not None
            and task.get_name() == _PERIODIC_SCHEDULER_AUTODELETE_TASK_NAME
        )

    async def _send_periodic_report_best_effort(
        self,
        *,
        bot,
        post_task_id: int,
    ) -> None:
        """Report only the inherited periodic fallback winner after terminal commit.

        The local delayed-delete task already owns its timer report and the mixed views
        observer owns its views report. Production names only the periodic deletion loop
        ``scheduler-autodelete``; restricting this compatibility report to that exact
        task keeps the three callers disjoint without changing DELETE authorization.
        """

        if not self._is_periodic_scheduler_autodelete_task():
            return

        try:
            async with self._session_factory() as session:
                post = await session.get(PostTask, int(post_task_id))
                if post is None:
                    await session.rollback()
                    return
                payload = dict(post.payload or {})
                if not bool(payload.get("autodelete_report", False)):
                    await session.rollback()
                    return
                channel = await session.get(Channel, int(post.channel_id))
                owner = (
                    await session.get(Client, int(channel.owner_id))
                    if channel is not None
                    else None
                )
                recipient = (
                    int(owner.tg_user_id)
                    if owner is not None and getattr(owner, "tg_user_id", None)
                    else None
                )
                link = payload.get("result_link")
                await session.rollback()

            if recipient is None:
                return
            text = "🗑️ Пост удалён по таймеру"
            if isinstance(link, str) and link:
                text = f"{text}\n{link}"
            await bot.send_message(
                chat_id=recipient,
                text=text,
                disable_web_page_preview=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reporting is deliberately best-effort and happens only after the
            # destructive result has been durably finalized as succeeded.
            return

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

        post_task_id = int(post_task_id)
        chat_id = int(chat_id)

        # Validate that the requested destructive target is exactly the target
        # persisted for this legacy occurrence.  This read does not elect a
        # winner; the unique INSERT below is the only authorization operation.
        async with self._session_factory() as session:
            post = await session.get(PostTask, post_task_id)
            if post is None:
                await session.rollback()
                return LegacyTimeViewsDeleteReserveResult("invalid_target")
            payload = dict(post.payload or {})
            if not self._is_legacy_mixed(payload):
                await session.rollback()
                return LegacyTimeViewsDeleteReserveResult("not_applicable")
            persisted_ids = self._normalized_ids(payload.get("result_ids"))
            if persisted_ids != requested_ids:
                await session.rollback()
                return LegacyTimeViewsDeleteReserveResult("invalid_target")
            channel = await session.get(Channel, int(post.channel_id))
            try:
                persisted_chat_id = int(channel.tg_chat_id) if channel is not None else None
            except (TypeError, ValueError):
                persisted_chat_id = None
            await session.rollback()
            if persisted_chat_id != chat_id:
                return LegacyTimeViewsDeleteReserveResult("invalid_target")

        fingerprint = self._target_fingerprint(
            post_task_id=post_task_id,
            chat_id=chat_id,
            message_ids=requested_ids,
        )
        token = uuid4().hex
        reservation = LegacyTimeViewsDeleteReservation(
            post_task_id=post_task_id,
            chat_id=chat_id,
            message_ids=requested_ids,
            target_fingerprint=fingerprint,
            token=token,
        )

        # This unique INSERT is the destructive CAS.  No earlier read can grant
        # provider authorization and the loser never receives the stored token.
        async with self._session_factory() as session:
            session.add(
                LegacyTimeViewsDeleteAction(
                    post_task_id=post_task_id,
                    chat_id=chat_id,
                    message_ids=list(requested_ids),
                    target_fingerprint=fingerprint,
                    reservation_token=token,
                    state="reserved",
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.execute(
                    select(LegacyTimeViewsDeleteAction.state).where(
                        LegacyTimeViewsDeleteAction.post_task_id == post_task_id
                    )
                )
                state = existing.scalar_one_or_none()
                if state is None:
                    raise
                return LegacyTimeViewsDeleteReserveResult(
                    self._classify_existing_state(str(state))
                )
            except Exception:
                await session.rollback()
                raise

        # The token becomes DELETE authorization only after the reservation
        # transaction has committed successfully and its session has closed.
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
        expected_fingerprint = self._target_fingerprint(
            post_task_id=reservation.post_task_id,
            chat_id=reservation.chat_id,
            message_ids=reservation.message_ids,
        )
        if expected_fingerprint != reservation.target_fingerprint:
            return False

        async with self._session_factory() as session:
            finalized_at = datetime.now(timezone.utc)
            result = await session.execute(
                update(LegacyTimeViewsDeleteAction)
                .where(
                    (LegacyTimeViewsDeleteAction.post_task_id == reservation.post_task_id)
                    & (LegacyTimeViewsDeleteAction.state == "reserved")
                    & (
                        LegacyTimeViewsDeleteAction.reservation_token
                        == reservation.token
                    )
                    & (
                        LegacyTimeViewsDeleteAction.target_fingerprint
                        == reservation.target_fingerprint
                    )
                    & (LegacyTimeViewsDeleteAction.chat_id == reservation.chat_id)
                )
                .values(state=state, finalized_at=finalized_at)
            )
            if result.rowcount != 1:
                await session.rollback()
                return False

            # Do not let an exact ledger finalize mark a subsequently changed
            # PostTask target as deleted.  A mismatch rolls back the ledger CAS,
            # leaving the durable reserved barrier in place.
            post = await session.get(PostTask, reservation.post_task_id)
            if post is None:
                await session.rollback()
                return False
            payload = dict(post.payload or {})
            if self._normalized_ids(payload.get("result_ids")) != reservation.message_ids:
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

            if state == "succeeded":
                payload["autodeleted"] = True
                payload["autodeleted_at"] = finalized_at.isoformat()
                post.payload = payload
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            return True

    async def delete_once(
        self,
        *,
        bot,
        post_task_id: int,
        chat_id: int,
        message_ids: list[int] | tuple[int, ...],
    ) -> LegacyTimeViewsDeleteOnceResult:
        reserved = await self.reserve(
            post_task_id=post_task_id,
            chat_id=chat_id,
            message_ids=message_ids,
        )
        if reserved.outcome != "reserved":
            return LegacyTimeViewsDeleteOnceResult(reserved.outcome)

        reservation = reserved.reservation
        if reservation is None:
            return LegacyTimeViewsDeleteOnceResult("unknown")

        # The provider call is deliberately outside every DB transaction.
        try:
            for message_id in reservation.message_ids:
                await bot.delete_message(
                    chat_id=reservation.chat_id,
                    message_id=int(message_id),
                )
        except Exception:
            try:
                await self.mark_unknown(reservation)
            except Exception:
                # A committed ``reserved`` row is already a no-replay barrier.
                pass
            return LegacyTimeViewsDeleteOnceResult(
                "provider_unknown", reservation=reservation
            )

        try:
            finalized = await self.mark_succeeded(reservation)
        except Exception:
            finalized = False
        if not finalized:
            # Successful provider DELETE with failed finalization remains
            # fail-closed: the committed reservation cannot be reclaimed.
            return LegacyTimeViewsDeleteOnceResult(
                "finalize_failed", reservation=reservation
            )

        await self._send_periodic_report_best_effort(
            bot=bot,
            post_task_id=reservation.post_task_id,
        )
        return LegacyTimeViewsDeleteOnceResult(
            "succeeded", reservation=reservation
        )
