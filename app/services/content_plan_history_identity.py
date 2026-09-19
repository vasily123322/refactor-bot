from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication
from app.services.publication_execution_mode import CANONICAL_EXECUTION_MODE


LEGACY_POST_TASK_CALLBACK_ID_META_KEY = "legacy_post_task_callback_id"


class HistoryPublicationIdentityKind(str, Enum):
    LEGACY_ONLY = "legacy_only"
    CANONICAL_LINKED = "canonical_linked"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class HistoryPublicationIdentity:
    kind: HistoryPublicationIdentityKind
    publication_id: int | None = None
    channel_id: int | None = None


async def resolve_history_publication_identity(
    session: AsyncSession,
    *,
    legacy_post_task_id: int,
) -> HistoryPublicationIdentity:
    """Resolve a supported historical callback alias without PostTask schema.

    The only supported historical identity is the durable alias written into
    Publication.metadata before P8. Missing aliases are expired; duplicates,
    malformed rows, or non-canonical ownership fail closed.
    """

    try:
        callback_id = int(legacy_post_task_id)
    except (TypeError, ValueError, OverflowError):
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    if callback_id <= 0:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)

    rows = list(
        (
            await session.execute(
                select(
                    Publication.id,
                    Publication.channel_id,
                    Publication.execution_mode,
                )
                .where(
                    Publication.meta[
                        LEGACY_POST_TASK_CALLBACK_ID_META_KEY
                    ].as_integer()
                    == callback_id
                )
                .order_by(Publication.id.asc())
                .limit(3)
            )
        ).all()
    )
    if not rows:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.LEGACY_ONLY)
    if len(rows) != 1 or rows[0].execution_mode != CANONICAL_EXECUTION_MODE:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)

    row = rows[0]
    try:
        publication_id = int(row.id)
        channel_id = int(row.channel_id)
    except (TypeError, ValueError, OverflowError):
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    if publication_id <= 0 or channel_id <= 0:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    return HistoryPublicationIdentity(
        HistoryPublicationIdentityKind.CANONICAL_LINKED,
        publication_id=publication_id,
        channel_id=channel_id,
    )
