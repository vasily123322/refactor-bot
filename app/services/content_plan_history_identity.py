from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication


class HistoryPublicationIdentityKind(str, Enum):
    LEGACY_ONLY = "legacy_only"
    CANONICAL_LINKED = "canonical_linked"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class HistoryPublicationIdentity:
    kind: HistoryPublicationIdentityKind
    publication_id: int | None = None


async def resolve_history_publication_identity(
    session: AsyncSession,
    *,
    legacy_post_task_id: int,
) -> HistoryPublicationIdentity:
    """Resolve a legacy history callback to canonical identity without reading PostTask.

    A missing Publication link is the explicit historical legacy fallback. Exactly one
    Publication link moves the detail read onto Publication identity. Multiple links are
    ambiguous and must never guess which canonical history row owns the callback.
    """

    try:
        task_id = int(legacy_post_task_id)
    except (TypeError, ValueError, OverflowError):
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    if task_id <= 0:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)

    raw_ids = list(
        (
            await session.execute(
                select(Publication.id)
                .where(Publication.legacy_post_task_id == task_id)
                .order_by(Publication.id.asc())
                .limit(2)
            )
        ).scalars().all()
    )
    if not raw_ids:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.LEGACY_ONLY)
    if len(raw_ids) != 1:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)

    try:
        publication_id = int(raw_ids[0])
    except (TypeError, ValueError, OverflowError):
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    if publication_id <= 0:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    return HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.CANONICAL_LINKED,
        publication_id=publication_id,
    )
