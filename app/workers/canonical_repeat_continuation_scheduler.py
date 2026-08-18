from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
    canonical_publication_delivery_repeat_started,
    canonical_publication_delivery_time_autodelete_started,
    canonical_publication_delivery_views_autodelete_started,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.canonical_publication_linked_repeat_parity import (
    CanonicalPublicationLinkedRepeatParityService,
)
from app.workers.canonical_recovery_scheduler import Scheduler as RecoveryScheduler
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


class Scheduler(RecoveryScheduler):
    """Recovery scheduler plus provider-free canonical repeat continuation lifecycle.

    Exact plain/silent fixed-delay linked repeats may now yield before the inherited
    legacy lease when a successfully started canonical repeat primary is live. The
    scheduler performs no repeat cutover mutation itself; the canonical primary remains
    the sole atomic handoff/claim owner. Unsupported repeat compositions continue through
    the inherited legacy callback.

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

    async def _yield_plain_repeat_to_canonical_primary(
        self,
        session: AsyncSession,
        *,
        task_id: int,
    ) -> bool:
        """Yield one exact plain/silent repeat without mutating transport authority."""

        if not canonical_publication_delivery_repeat_started():
            return False

        publications = list(
            (
                await session.execute(
                    select(Publication)
                    .where(Publication.legacy_post_task_id == int(task_id))
                    .limit(2)
                )
            ).scalars().all()
        )
        if len(publications) != 1:
            return False
        publication = publications[0]
        if publication.schedule_entry_id is None:
            return False

        task = await session.get(PostTask, int(task_id), populate_existing=True)
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id),
            populate_existing=True,
        )
        if task is None or schedule is None:
            return False

        plan = await CanonicalPublicationDeliveryPlanner(session).plan(int(publication.id))
        if plan is None:
            return False
        try:
            runtime_options = plan.runtime_options()
        except (TypeError, ValueError):
            return False
        if not isinstance(runtime_options, dict) or not set(runtime_options).issubset(
            {"silent"}
        ):
            return False

        proof = CanonicalPublicationLinkedRepeatParityService().prove(
            task=task,
            publication=publication,
            schedule=schedule,
            plan=plan,
        )
        if proof is None:
            return False
        if (
            proof.pin_on
            or proof.forward_channel_ids
            or proof.time_autodelete_seconds is not None
            or proof.views_autodelete_threshold is not None
            or proof.autodelete_report
            or proof.views_pin_forward_composed
        ):
            return False

        logger.info(
            "Scheduler: yielding exact plain repeat to canonical primary post_id={} publication_id={} repeat_group_id={}",
            int(task_id),
            int(publication.id),
            int(proof.repeat_group_id),
        )
        return True

    async def _retire_transport_for_canonical_primary(
        self,
        session: AsyncSession,
        *,
        task_id: int,
    ) -> bool:
        """Atomically hand an exact started-capability transport to canonical primary."""

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
        ).retire_for_canonical_delivery(
            int(publication_ids[0]),
            allow_time_autodelete=(
                canonical_publication_delivery_time_autodelete_started()
            ),
            allow_views_autodelete=(
                canonical_publication_delivery_views_autodelete_started()
            ),
            allow_forward=True,
        )
        if result.outcome != "retired":
            return False

        logger.info(
            "Scheduler: retired legacy transport for canonical primary post_id={} publication_id={}",
            int(task_id),
            int(publication_ids[0]),
        )
        return True

    async def _mark_processing(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        """Yield/retire exact canonical work before inherited legacy lease claim."""

        if not items or not canonical_publication_delivery_primary_started():
            await super()._mark_processing(session, items)
            return

        selected_ids = [int(post.id) for post in items]
        legacy_candidates: list[PostTask] = []
        for task_id in selected_ids:
            try:
                yielded_repeat = await self._yield_plain_repeat_to_canonical_primary(
                    session,
                    task_id=task_id,
                )
                if yielded_repeat:
                    continue
                retired = await self._retire_transport_for_canonical_primary(
                    session,
                    task_id=task_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await session.rollback()
                logger.warning(
                    "Scheduler: canonical authority retirement failed post_id={} type={}",
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
