from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication


class CanonicalLinkedControlProbe:
    """Read-only probe for legacy UI controls backed by a linked PostTask.

    A linked PostTask is still a compatibility/execution adapter while legacy fallback is
    possible, but its payload is no longer an independent control plane. Controls that
    only rewrite PostTask must fail closed rather than create canonical/legacy intent
    drift. This probe performs no locks, writes, commits, leases, or provider calls.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def publication_id_for_task(self, post_task_id: int) -> int | None:
        try:
            task_id = int(post_task_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if task_id <= 0:
            return None
        rows = list(
            (
                await self.session.execute(
                    select(Publication.id)
                    .where(Publication.legacy_post_task_id == task_id)
                    .limit(2)
                )
            ).scalars().all()
        )
        if len(rows) != 1:
            return None
        return int(rows[0])
