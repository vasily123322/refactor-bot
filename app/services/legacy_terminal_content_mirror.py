from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.services.legacy_content_mirror import mirror_legacy_post_task


_TERMINAL_TASK_STATUSES = ("done", "failed", "skipped", "cancelled")


async def mirror_unlinked_terminal_legacy_tasks(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> tuple[int, int]:
    """Mirror only historical terminal PostTask rows into canonical read state.

    Active unlinked work is intentionally excluded. After atomic schedule mirroring,
    a new supported canonical profile should never need an asynchronous active backfill.
    Mirroring an old pending/processing row while the legacy scheduler can claim or is
    already executing it would create a second canonical execution authority. Let that
    historical legacy occurrence finish under its existing owner and mirror it only
    after the outcome is terminal.
    """

    linked_subquery = select(Publication.legacy_post_task_id).where(
        Publication.legacy_post_task_id.is_not(None)
    )
    result = await session.execute(
        select(PostTask)
        .where(
            PostTask.id.not_in(linked_subquery),
            PostTask.status.in_(_TERMINAL_TASK_STATUSES),
        )
        .order_by(PostTask.id.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    tasks = list(result.scalars().all())
    mirrored = 0
    skipped = 0
    for task in reversed(tasks):
        try:
            publication = await mirror_legacy_post_task(session, task)
            if publication is None:
                skipped += 1
            else:
                mirrored += 1
        except Exception:
            skipped += 1
            logger.exception(
                "Terminal legacy content mirror failed PostTask id={}",
                int(task.id),
            )
    return mirrored, skipped
