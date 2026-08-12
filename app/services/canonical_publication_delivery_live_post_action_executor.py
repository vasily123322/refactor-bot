from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_action_ledger import (
    CanonicalPublicationDeliveryActionLedger,
    CanonicalPublicationDeliveryActionReservation,
)
from app.services.canonical_publication_delivery_live_post_actions import (
    CanonicalPublicationDeliveryLivePostActionPlanner,
    CanonicalPublicationLivePostAction,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryPostActionExecution:
    planned: int = 0
    reserved: int = 0
    skipped_reserved: int = 0
    succeeded: int = 0
    unknown: int = 0
    suppressed: int = 0
    conflicts: int = 0


class CanonicalPublicationDeliveryLivePostActionExecutor:
    """Execute live pin/forward actions with durable at-most-once reservations.

    Only a newly committed ledger reservation authorizes a provider call. Every such
    reservation is followed by a fresh live planner pass before provider invocation.
    Drift in that second proof marks the reservation `suppressed` and aborts the chain.

    Provider failures are best-effort like the legacy scheduler, but the deterministic
    key is never reopened: success becomes `succeeded`, generic provider failure becomes
    `unknown`, and cancellation is marked unknown before propagation when possible.
    """

    def __init__(
        self,
        *,
        bot,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory

    async def _plan(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ):
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryLivePostActionPlanner(
                session
            ).plan(context)

    async def _reserve(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        action: CanonicalPublicationLivePostAction,
    ):
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryActionLedger(session).reserve(
                context.lease,
                action_key=action.action_key,
                action_type=action.action_type,
                intent_fingerprint=action.intent_fingerprint,
            )

    async def _mark(
        self,
        reservation: CanonicalPublicationDeliveryActionReservation,
        state: str,
    ) -> bool:
        async with self.session_factory() as session:
            ledger = CanonicalPublicationDeliveryActionLedger(session)
            if state == "succeeded":
                return await ledger.mark_succeeded(reservation)
            if state == "unknown":
                return await ledger.mark_unknown(reservation)
            if state == "suppressed":
                return await ledger.mark_suppressed(reservation)
            raise ValueError("unsupported canonical delivery action state")

    async def _best_effort_mark(
        self,
        reservation: CanonicalPublicationDeliveryActionReservation,
        state: str,
    ) -> None:
        try:
            await asyncio.shield(self._mark(reservation, state))
        except asyncio.CancelledError:
            # The outer cancellation still propagates. If this shielded mark cannot
            # complete, the durable `reserved` row itself remains a no-retry barrier.
            raise
        except Exception as exc:
            logger.warning(
                "Canonical publication post-action ledger mark failed publication_id={} action_type={} error_type={}",
                int(reservation.publication_id),
                str(reservation.action_type),
                type(exc).__name__,
            )

    @staticmethod
    def _find_exact(
        actions: tuple[CanonicalPublicationLivePostAction, ...],
        reserved: CanonicalPublicationLivePostAction,
    ) -> CanonicalPublicationLivePostAction | None:
        for action in actions:
            if action.action_key != reserved.action_key:
                continue
            if (
                action.action_type == reserved.action_type
                and action.intent_fingerprint == reserved.intent_fingerprint
            ):
                return action
            return None
        return None

    async def _provider_call(self, action: CanonicalPublicationLivePostAction) -> None:
        if action.action_type == "pin":
            await self.bot.pin_chat_message(
                chat_id=int(action.source_telegram_chat_id),
                message_id=int(action.source_message_id),
            )
            return
        if action.action_type == "forward":
            if action.target_telegram_chat_id is None:
                raise RuntimeError("forward target missing")
            await self.bot.forward_message(
                chat_id=int(action.target_telegram_chat_id),
                from_chat_id=int(action.source_telegram_chat_id),
                message_id=int(action.source_message_id),
                disable_notification=bool(action.disable_notification),
            )
            return
        raise RuntimeError("unsupported canonical post action")

    async def execute(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> CanonicalPublicationDeliveryPostActionExecution:
        initial = await self._plan(context)
        if initial is None or not initial.actions:
            return CanonicalPublicationDeliveryPostActionExecution(
                planned=0 if initial is None else len(initial.actions)
            )

        counts = {
            "planned": len(initial.actions),
            "reserved": 0,
            "skipped_reserved": 0,
            "succeeded": 0,
            "unknown": 0,
            "suppressed": 0,
            "conflicts": 0,
        }

        for action in initial.actions:
            reservation_result = await self._reserve(context, action)
            if reservation_result.outcome == "already_reserved":
                counts["skipped_reserved"] += 1
                continue
            if reservation_result.outcome != "reserved" or reservation_result.reservation is None:
                counts["conflicts"] += 1
                break

            reservation = reservation_result.reservation
            counts["reserved"] += 1

            # Fresh authority + exact target/fingerprint proof after reservation commit.
            try:
                fresh = await self._plan(context)
            except asyncio.CancelledError:
                await self._best_effort_mark(reservation, "suppressed")
                raise
            if fresh is None:
                await self._best_effort_mark(reservation, "suppressed")
                counts["suppressed"] += 1
                break
            exact = self._find_exact(fresh.actions, action)
            if exact is None:
                await self._best_effort_mark(reservation, "suppressed")
                counts["suppressed"] += 1
                break

            provider_started = datetime.now(timezone.utc)
            try:
                await self._provider_call(exact)
            except asyncio.CancelledError:
                # Provider side effect is now ambiguous. Never reopen the key.
                try:
                    await self._best_effort_mark(reservation, "unknown")
                finally:
                    raise
            except Exception as exc:
                logger.warning(
                    "Canonical publication post action failed publication_id={} action_type={} error_type={}",
                    int(context.publication_id),
                    str(action.action_type),
                    type(exc).__name__,
                )
                await self._best_effort_mark(reservation, "unknown")
                counts["unknown"] += 1
                continue

            try:
                await self._best_effort_mark(reservation, "succeeded")
            except asyncio.CancelledError:
                # The provider already returned success. Reservation remains a durable
                # no-retry barrier even if terminal evidence marking is interrupted.
                raise
            counts["succeeded"] += 1
            logger.debug(
                "Canonical publication post action completed publication_id={} action_type={} elapsed_ms={}",
                int(context.publication_id),
                str(action.action_type),
                max(
                    0,
                    int(
                        (datetime.now(timezone.utc) - provider_started).total_seconds()
                        * 1000
                    ),
                ),
            )

        return CanonicalPublicationDeliveryPostActionExecution(**counts)
