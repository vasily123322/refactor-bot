from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.workers.canonical_recovery_scheduler import Scheduler as RecoveryScheduler
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


class Scheduler(RecoveryScheduler):
    """Recovery scheduler plus provider-free canonical repeat continuation lifecycle.

    The existing successful-repeat flag remains the single operator intent. Legacy-linked
    successful repeats continue through the inherited scheduler callback; transport-
    retired canonical-delivered repeat sources are disjoint and handled by the child
    continuation worker.

    Production constructs the scheduler with an async session factory. A single long-lived
    AsyncSession cannot safely back an independent polling worker, so continuation stays
    unavailable in that unsupported construction shape rather than sharing a session.
    """

    def __init__(
        self,
        *args,
        repeat_continuation_enabled: bool | None = None,
        continuation_worker_factory: Callable[..., CanonicalRepeatContinuationWorker] = (
            CanonicalRepeatContinuationWorker
        ),
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._repeat_continuation_enabled = (
            bool(settings.canonical_repeat_successful_planning_enabled)
            if repeat_continuation_enabled is None
            else bool(repeat_continuation_enabled)
        )
        self._continuation_worker_factory = continuation_worker_factory
        self._repeat_continuation_worker: CanonicalRepeatContinuationWorker | None = None

    @property
    def repeat_continuation_available(self) -> bool:
        return self._repeat_continuation_worker is not None

    def _continuation_session_factory(
        self,
    ) -> async_sessionmaker[AsyncSession] | None:
        factory = getattr(self, "session_factory", None)
        return factory if factory is not None else None

    async def _retire_plain_transport_for_canonical_primary(
        self,
        session: AsyncSession,
        *,
        task_id: int,
    ) -> bool:
        """Atomically hand exact-parity plain transport to a started canonical primary."""

        if not canonical_publication_delivery_primary_started():
            return False

        publication_ids = list(
            (
                await session.execute(
                    select(Publication.id)
                    .where(Publication.legacy_post_task_id == int(task_id))
                    .limit(2)
                )
            ).scalars().all()
        )
        if len(publication_ids) != 1:
            return False

        result = await CanonicalPublicationLegacyTransportHandoffService(
            session
        ).retire_for_canonical_delivery(int(publication_ids[0]))
        if result.outcome != "retired":
            return False

        logger.info(
            "Scheduler: retired legacy plain transport for canonical primary post_id={} publication_id={}",
            int(task_id),
            int(publication_ids[0]),
        )
        return True

    async def _mark_processing(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        """Retire exact canonical plain work before inherited legacy lease claim."""

        if not items or not canonical_publication_delivery_primary_started():
            await super()._mark_processing(session, items)
            return

        selected_ids = [int(post.id) for post in items]
        legacy_candidates: list[PostTask] = []
        for task_id in selected_ids:
            try:
                retired = await self._retire_plain_transport_for_canonical_primary(
                    session,
                    task_id=task_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await session.rollback()
                logger.warning(
                    "Scheduler: canonical plain transport retirement failed post_id={} type={}",
                    task_id,
                    type(exc).__name__,
                )
                retired = False

            if retired:
                continue

            # Handoff failures deliberately roll back their temporary cutover CAS.
            # Reload after that transaction boundary before preserving legacy fallback.
            post = await session.get(PostTask, task_id, populate_existing=True)
            if post is not None:
                legacy_candidates.append(post)

        items[:] = legacy_candidates
        await super()._mark_processing(session, items)

    async def start(self) -> None:
        await super().start()
        if not self._repeat_continuation_enabled:
            return

        session_factory = self._continuation_session_factory()
        if session_factory is None:
            logger.warning(
                "Canonical repeat continuation requested but scheduler has no session factory"
            )
            return

        worker = self._continuation_worker_factory(session_factory=session_factory)
        try:
            await worker.start()
        except BaseException:
            try:
                await worker.stop()
            except Exception:
                logger.exception(
                    "Boot: failed to clean up canonical repeat continuation worker after startup failure"
                )
            await super().stop()
            raise
        self._repeat_continuation_worker = worker

    async def stop(self) -> None:
        worker = self._repeat_continuation_worker
        self._repeat_continuation_worker = None
        if worker is not None:
            try:
                await worker.stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shutdown: failed to stop canonical repeat continuation")
        await super().stop()
