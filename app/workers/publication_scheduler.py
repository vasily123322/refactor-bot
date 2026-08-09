from __future__ import annotations

from loguru import logger
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.reliable_scheduler import Scheduler as ReliableScheduler


class Scheduler(ReliableScheduler):
    """Reliable scheduler with atomic claim and Publication-domain projection.

    PostTask remains the compatibility transport during migration, but normal
    scheduler state changes are projected immediately into Publication/ScheduleEntry.
    The periodic reconciler remains a recovery/backfill path for missed updates.
    """

    async def _project_publication(
        self,
        session: AsyncSession,
        post: PostTask,
    ) -> None:
        task_id = int(post.id)
        try:
            if self.session_factory is not None:
                async with self.session_factory() as projection_session:
                    projection_task = await projection_session.get(PostTask, task_id)
                    if projection_task is None:
                        return
                    publication = await LegacyPublicationBridge(
                        projection_session
                    ).reconcile_task(projection_task)
            else:
                publication = await LegacyPublicationBridge(session).reconcile_task(post)
        except Exception as exc:
            if self.session_factory is None:
                await self._rollback(session, "publication projection")
            logger.warning(
                "Scheduler: publication projection failed post_id={} type={}",
                task_id,
                type(exc).__name__,
            )
            return

        if publication is not None:
            logger.trace(
                "Scheduler: projected post_id={} publication_id={} status={}",
                task_id,
                int(publication.id),
                publication.status,
            )

    async def _mark_processing(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        """Atomically claim only tasks that are still pending in the database.

        The scheduler SELECT and claim are separate operations for compatibility
        across SQLite/PostgreSQL. Another process may therefore have claimed a row
        after this worker selected it. Each compare-and-set UPDATE includes the
        expected pending state; losers are removed from the in-memory batch before
        any Telegram side effect can run.
        """
        if not items:
            return

        selected = list(items)
        claimed: list[PostTask] = []
        try:
            for post in selected:
                result = await session.execute(
                    update(PostTask)
                    .where(
                        PostTask.id == int(post.id),
                        PostTask.status == "pending",
                    )
                    .values(status="processing")
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0) or 0) == 1:
                    claimed.append(post)
            await session.commit()
        except Exception:
            await self._rollback(session, "atomic task claim")
            logger.exception(
                "Scheduler: atomic claim failed selected={}",
                len(selected),
            )
            raise

        items[:] = claimed
        for post in claimed:
            post.status = "processing"
            await self._project_publication(session, post)

        skipped = len(selected) - len(claimed)
        if skipped:
            logger.info(
                "Scheduler: claim contention selected={} claimed={} skipped={}",
                len(selected),
                len(claimed),
                skipped,
            )

    async def _process_items(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        await super()._process_items(session, items)
        for post in items:
            await self._project_publication(session, post)
