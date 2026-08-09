from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.services.candidate_enrichment import (
    CandidateEnrichmentBusy,
    CandidateEnrichmentError,
    CandidateEnrichmentService,
    LocalCandidateEnricher,
)


@dataclass(frozen=True, slots=True)
class LocalEnrichmentTick:
    selected: int = 0
    attempted: int = 0
    completed: int = 0
    reused: int = 0
    skipped_busy: int = 0
    failures: int = 0
    timeouts: int = 0


class LocalCandidateEnrichmentWorker:
    """Bounded deterministic enrichment for untouched Inbox candidates.

    The worker intentionally targets only candidates whose summary/topic/score are
    all still empty. It therefore never overwrites AI/manual enrichment. Candidate
    concurrency and stale-run recovery remain owned by CandidateEnrichmentService.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: float = 15.0,
        batch_size: int = 20,
        candidate_timeout_seconds: float = 10.0,
    ) -> None:
        self.session_factory = session_factory
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.batch_size = max(1, min(int(batch_size), 100))
        self.candidate_timeout_seconds = max(1.0, min(float(candidate_timeout_seconds), 60.0))
        self.provider = LocalCandidateEnricher()
        self._task: asyncio.Task[None] | None = None

    async def _candidate_keys(self) -> list[tuple[int, int]]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(ContentCandidate.channel_id, ContentCandidate.id)
                .join(
                    SourceDocument,
                    SourceDocument.id == ContentCandidate.source_document_id,
                )
                .where(
                    ContentCandidate.status == "new",
                    ContentCandidate.summary.is_(None),
                    ContentCandidate.topic.is_(None),
                    ContentCandidate.score.is_(None),
                    SourceDocument.content != "",
                )
                .order_by(ContentCandidate.created_at.asc(), ContentCandidate.id.asc())
                .limit(self.batch_size)
            )
            return [(int(channel_id), int(candidate_id)) for channel_id, candidate_id in rows.all()]

    async def _enrich_one(self, channel_id: int, candidate_id: int):
        async with self.session_factory() as session:
            return await CandidateEnrichmentService(session).enrich(
                channel_id=channel_id,
                candidate_id=candidate_id,
                provider=self.provider,
            )

    async def run_once(self) -> LocalEnrichmentTick:
        keys = await self._candidate_keys()
        completed = 0
        reused = 0
        skipped_busy = 0
        failures = 0
        timeouts = 0

        for channel_id, candidate_id in keys:
            try:
                result = await asyncio.wait_for(
                    self._enrich_one(channel_id, candidate_id),
                    timeout=self.candidate_timeout_seconds,
                )
                if result.reused_existing:
                    reused += 1
                else:
                    completed += 1
            except CandidateEnrichmentBusy:
                skipped_busy += 1
            except TimeoutError:
                timeouts += 1
                logger.warning("Local enrichment worker: candidate enrichment timed out")
            except CandidateEnrichmentError:
                failures += 1
                logger.warning("Local enrichment worker: candidate enrichment failed")
            except Exception:
                failures += 1
                logger.exception("Local enrichment worker: unexpected candidate failure")

        return LocalEnrichmentTick(
            selected=len(keys),
            attempted=len(keys),
            completed=completed,
            reused=reused,
            skipped_busy=skipped_busy,
            failures=failures,
            timeouts=timeouts,
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            local_enrichment_worker_loop(
                self,
                interval_seconds=self.interval_seconds,
            ),
            name="local-candidate-enrichment-worker",
        )
        logger.info(
            "Local enrichment worker started: interval={}s batch_size={}",
            self.interval_seconds,
            self.batch_size,
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("Local enrichment worker stopped")


async def local_enrichment_worker_loop(
    worker: LocalCandidateEnrichmentWorker,
    *,
    interval_seconds: float = 15.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run bounded ticks until cancellation; cancellation is never swallowed."""
    delay = max(1.0, float(interval_seconds))
    while True:
        try:
            tick = await worker.run_once()
            if tick.selected:
                logger.info(
                    "Local enrichment worker tick: selected={} completed={} reused={} busy={} failed={} timeouts={}",
                    tick.selected,
                    tick.completed,
                    tick.reused,
                    tick.skipped_busy,
                    tick.failures,
                    tick.timeouts,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Local enrichment worker tick failed")
        await sleep(delay)
