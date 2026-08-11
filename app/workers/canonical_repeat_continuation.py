from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.canonical_repeat_transport_adapter import (
    CanonicalRepeatTransportAdapter,
)
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatContinuationTick:
    scanned: int = 0
    eligible: int = 0
    reserved: int = 0
    materialized: int = 0
    already_complete: int = 0
    ineligible: int = 0
    conflicts: int = 0
    failures: int = 0
    cursor_reset: bool = False


def _positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _repeat_enabled(schedule: ScheduleEntry) -> bool:
    rule = schedule.repeat_rule
    if not isinstance(rule, Mapping):
        return False
    return rule.get("enabled") is True and _positive_int(rule.get("seconds")) is not None


def _canonical_attempt(attempt: PublicationAttempt) -> bool:
    return isinstance(attempt.meta, Mapping) and dict(attempt.meta).get(
        "canonical_delivery"
    ) is True


class CanonicalRepeatContinuationWorker:
    """Recover successor creation for canonical-delivered repeat sources.

    The historical Scheduler invokes canonical repeat planning only while it still owns a
    successful legacy PostTask. Once a repeat occurrence is delivered canonically and its
    compatibility transport is retired, that callback no longer exists. This worker is
    the provider-free continuation/recovery owner for exactly those sources.

    It never touches linked legacy-owned rows. Candidate authority requires terminal
    canonical delivery evidence, physical transport retirement and an enabled repeat
    rule. Existing repeat primitives remain the only planning/materialization authority.

    Retry order is deliberately verifier-first:
      * matched  -> already complete;
      * pending  -> materialize the already-reserved slot without replanning;
      * ineligible -> reserve the first future slot, then materialize;
      * conflict -> fail closed.

    This order is critical after crashes: a reservation may refer to a slot that has since
    passed. Replanning from a later wall clock would choose a different slot and conflict
    with the durable reservation; verifier-first recovery preserves the original plan.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: int = 15,
        batch_size: int = 25,
        scan_limit: int = 500,
    ) -> None:
        self.session_factory = session_factory
        self.interval_seconds = max(1, min(int(interval_seconds), 3600))
        self.batch_size = max(1, min(int(batch_size), 200))
        self.scan_limit = max(self.batch_size, min(int(scan_limit), 1000))
        self._cursor: int | None = None
        self._loop = PollingLoop(
            interval_seconds=self.interval_seconds,
            on_tick=self._tick,
            name="canonical-repeat-continuation",
        )

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def _select(self) -> tuple[list[int], int, bool]:
        after_id = int(self._cursor or 0)
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Publication, ScheduleEntry, PublicationAttempt)
                    .join(
                        ScheduleEntry,
                        and_(
                            ScheduleEntry.id == Publication.schedule_entry_id,
                            ScheduleEntry.channel_id == Publication.channel_id,
                            ScheduleEntry.content_item_id == Publication.content_item_id,
                            ScheduleEntry.content_revision == Publication.content_revision,
                        ),
                    )
                    .join(
                        PublicationAttempt,
                        and_(
                            PublicationAttempt.publication_id == Publication.id,
                            PublicationAttempt.attempt == Publication.attempt_count,
                        ),
                    )
                    .where(
                        Publication.id > after_id,
                        Publication.legacy_post_task_id.is_(None),
                        Publication.status == "published",
                        ScheduleEntry.status == "completed",
                        PublicationAttempt.status == "published",
                        PublicationAttempt.finished_at.is_not(None),
                    )
                    .order_by(Publication.id.asc())
                    .limit(self.scan_limit)
                )
            ).all()

        ids: list[int] = []
        for publication, schedule, attempt in rows:
            if not _canonical_attempt(attempt) or not _repeat_enabled(schedule):
                continue
            ids.append(int(publication.id))
            if len(ids) >= self.batch_size:
                break

        scanned = len(rows)
        next_cursor = int(rows[-1][0].id) if rows else after_id
        done = scanned < self.scan_limit
        return ids, next_cursor, done

    async def _verify(self, publication_id: int):
        async with self.session_factory() as session:
            return await CanonicalRepeatReservationVerifier(session).verify(publication_id)

    async def _reserve(self, publication_id: int, *, now: datetime):
        async with self.session_factory() as session:
            return await CanonicalRepeatPlanReservationService(session).reserve_next(
                publication_id,
                after=now,
            )

    async def _materialize(self, publication_id: int):
        async with self.session_factory() as session:
            return await CanonicalRepeatTransportAdapter(session).materialize(publication_id)

    async def _continue_one(self, publication_id: int, *, now: datetime) -> str:
        verification = await self._verify(publication_id)
        if verification.outcome == "matched":
            return "already_complete"
        if verification.outcome == "conflict":
            return "conflict"

        if verification.outcome == "ineligible":
            reservation = await self._reserve(publication_id, now=now)
            if reservation.outcome == "conflict":
                return "conflict"
            if reservation.outcome == "existing_successor":
                # A canonical source cannot legitimately have an unreserved successor
                # under this cutover. Do not adopt ambiguous external/legacy state.
                return "conflict"
            if reservation.outcome not in {"reserved", "already_reserved"}:
                return "ineligible"
            reserved = reservation.outcome == "reserved"
        else:
            # pending means a durable reservation already exists; never replan it.
            reserved = False

        materialized = await self._materialize(publication_id)
        if materialized.outcome not in {"created", "existing", "existing_transport"}:
            return "conflict" if materialized.outcome == "conflict" else "ineligible"

        verified = await self._verify(publication_id)
        if verified.outcome != "matched":
            return "conflict" if verified.outcome == "conflict" else "ineligible"
        if materialized.outcome == "created":
            return "reserved_and_materialized" if reserved else "materialized"
        return "already_complete"

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> CanonicalRepeatContinuationTick:
        current = as_utc(now or datetime.now(timezone.utc))
        publication_ids, next_cursor, done = await self._select()
        counts = {
            "reserved": 0,
            "materialized": 0,
            "already_complete": 0,
            "ineligible": 0,
            "conflicts": 0,
            "failures": 0,
        }

        for publication_id in publication_ids:
            try:
                outcome = await self._continue_one(publication_id, now=current)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                counts["failures"] += 1
                logger.warning(
                    "Canonical repeat continuation failed source_publication_id={} type={}",
                    publication_id,
                    type(exc).__name__,
                )
                continue

            if outcome == "reserved_and_materialized":
                counts["reserved"] += 1
                counts["materialized"] += 1
            elif outcome == "materialized":
                counts["materialized"] += 1
            elif outcome == "already_complete":
                counts["already_complete"] += 1
            elif outcome == "conflict":
                counts["conflicts"] += 1
            else:
                counts["ineligible"] += 1

        self._cursor = None if done else next_cursor
        return CanonicalRepeatContinuationTick(
            scanned=(0 if not publication_ids and next_cursor == int(self._cursor or 0) else max(len(publication_ids), 0)),
            eligible=len(publication_ids),
            cursor_reset=done,
            **counts,
        )

    async def _tick(self) -> None:
        tick = await self.run_once()
        if tick.materialized or tick.conflicts or tick.failures:
            logger.info(
                "Canonical repeat continuation: eligible={} reserved={} materialized={} "
                "already_complete={} ineligible={} conflicts={} failures={} cursor_reset={}",
                tick.eligible,
                tick.reserved,
                tick.materialized,
                tick.already_complete,
                tick.ineligible,
                tick.conflicts,
                tick.failures,
                tick.cursor_reset,
            )
