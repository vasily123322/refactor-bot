from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.reliable_scheduler import Scheduler as ReliableScheduler


class Scheduler(ReliableScheduler):
    """Reliable scheduler with synchronous Publication-domain projection.

    PostTask remains the compatibility transport during migration, but normal
    scheduler state changes are projected immediately into Publication/ScheduleEntry.
    The periodic reconciler remains a recovery/backfill path for missed updates.
    """

    async def _project_publication(
        self,
        session: AsyncSession,
        post: PostTask,
    ) -> None:
        try:
            publication = await LegacyPublicationBridge(session).reconcile_task(post)
        except Exception as exc:
            await self._rollback(session, "publication projection")
            logger.warning(
                "Scheduler: publication projection failed post_id={} type={}",
                int(post.id),
                type(exc).__name__,
            )
            return

        if publication is not None:
            logger.trace(
                "Scheduler: projected post_id={} publication_id={} status={}",
                int(post.id),
                int(publication.id),
                publication.status,
            )

    async def _mark_processing(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        await super()._mark_processing(session, items)
        for post in items:
            # The parent implementation uses a bulk UPDATE. Keep the identity-map
            # object aligned so task-scoped reconciliation sees `processing` even on
            # SQLAlchemy backends/configurations that do not synchronize that UPDATE.
            post.status = "processing"
            await self._project_publication(session, post)

    async def _process_items(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        await super()._process_items(session, items)
        for post in items:
            await self._project_publication(session, post)
