from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.runtime_safety import CanonicalRuntimeSafetyAudit


NO_REPLAY_STATE = "no_replay"


async def has_no_replay_barrier(
    session: AsyncSession,
    *,
    publication_id: int,
) -> bool:
    """Return True when durable migrated evidence forbids automatic provider replay."""

    try:
        safe_publication_id = int(publication_id)
    except (TypeError, ValueError, OverflowError):
        return True
    if safe_publication_id <= 0:
        return True
    row_id = await session.scalar(
        select(CanonicalRuntimeSafetyAudit.id)
        .where(
            CanonicalRuntimeSafetyAudit.publication_id == safe_publication_id,
            CanonicalRuntimeSafetyAudit.state == NO_REPLAY_STATE,
        )
        .limit(1)
    )
    return row_id is not None
