from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidateCursor,
    CanonicalPublicationDeliveryCandidateSelector,
)


class CanonicalPublicationDeliveryExecutionResultLike(Protocol):
    outcome: str


class CanonicalPublicationDeliveryExecutorLike(Protocol):
    async def execute(
        self,
        publication_id: int,
    ) -> CanonicalPublicationDeliveryExecutionResultLike: ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryWorkerTick:
    selected: int = 0
    published: int = 0
    failed: int = 0
    ineligible: int = 0
    lease_lost: int = 0
    unexpected: int = 0
    failures: int = 0
    cursor_reset: bool = False


def _bounded_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


class CanonicalPublicationDeliveryWorker:
    """Bounded transport-independent orchestration for canonical delivery candidates.

    Candidate discovery advances an in-process keyset cursor with ``scan_page`` so
    planner-invalid front rows cannot starve later valid work. Cursor advancement is
    committed only after the whole selected batch finishes; cancellation therefore
    re-scans from the previous cursor instead of skipping an unprocessed candidate.

    The injected executor remains the sole claim/provider/finalization authority; this
    worker never sends Telegram messages itself and never retries an individual
    candidate after an ambiguous executor result inside the same tick.
    """

    def __init__(
        self,
        *,
        executor: CanonicalPublicationDeliveryExecutorLike,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: int = 5,
        batch_size: int = 25,
        scan_limit: int = 500,
    ) -> None:
        self.executor = executor
        self.session_factory = session_factory
        self.interval_seconds = _bounded_int(
            interval_seconds,
            default=5,
            minimum=1,
            maximum=3600,
        )
        self.batch_size = _bounded_int(
            batch_size,
            default=25,
            minimum=1,
            maximum=500,
        )
        self.scan_limit = _bounded_int(
            scan_limit,
            default=500,
            minimum=1,
            maximum=500,
        )
        self._cursor: CanonicalPublicationDeliveryCandidateCursor | None = None
        self._loop = PollingLoop(
            interval_seconds=self.interval_seconds,
            on_tick=self._tick,
            name="canonical-publication-delivery",
        )

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def _select(self):
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryCandidateSelector(session).scan_page(
                limit=self.batch_size,
                scan_limit=self.scan_limit,
                after=self._cursor,
            )

    async def run_once(self) -> CanonicalPublicationDeliveryWorkerTick:
        batch = await self._select()
        cursor_reset = bool(batch.done)

        counts = {
            "published": 0,
            "failed": 0,
            "ineligible": 0,
            "lease_lost": 0,
            "unexpected": 0,
            "failures": 0,
        }
        for candidate in batch.candidates:
            publication_id = int(candidate.publication_id)
            try:
                result = await self.executor.execute(publication_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                counts["failures"] += 1
                logger.warning(
                    "Canonical publication delivery worker candidate failed "
                    "publication_id={} error_type={}",
                    publication_id,
                    type(exc).__name__,
                )
                continue

            outcome = str(getattr(result, "outcome", ""))
            if outcome in {"published", "failed", "ineligible", "lease_lost"}:
                counts[outcome] += 1
            else:
                counts["unexpected"] += 1
                logger.warning(
                    "Canonical publication delivery worker returned unexpected outcome "
                    "publication_id={}",
                    publication_id,
                )

        self._cursor = None if batch.done else batch.next_cursor
        return CanonicalPublicationDeliveryWorkerTick(
            selected=len(batch.candidates),
            cursor_reset=cursor_reset,
            **counts,
        )

    async def _tick(self) -> None:
        tick = await self.run_once()
        if (
            tick.published
            or tick.failed
            or tick.lease_lost
            or tick.unexpected
            or tick.failures
        ):
            logger.info(
                "Canonical publication delivery: selected={} published={} failed={} "
                "ineligible={} lease_lost={} unexpected={} failures={} cursor_reset={}",
                tick.selected,
                tick.published,
                tick.failed,
                tick.ineligible,
                tick.lease_lost,
                tick.unexpected,
                tick.failures,
                tick.cursor_reset,
            )
