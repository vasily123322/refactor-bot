from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
)


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
    """Resolve a legacy callback key to durable canonical identity without PostTask.

    The compatibility key may still be the live ``legacy_post_task_id`` link or the
    narrow durable alias written before that link is retired. Canonical ownership is
    accepted only when exactly one persisted ``execution_mode=canonical`` Publication
    owns the key. Intentional legacy remains on PostTask; malformed, mixed or duplicate
    ownership fails closed. No channel/time/content heuristic participates.
    """

    try:
        task_id = int(legacy_post_task_id)
    except (TypeError, ValueError, OverflowError):
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    if task_id <= 0:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)

    rows = list(
        (
            await session.execute(
                select(
                    Publication.id,
                    Publication.channel_id,
                    Publication.execution_mode,
                    Publication.legacy_post_task_id,
                )
                .where(
                    or_(
                        Publication.legacy_post_task_id == task_id,
                        Publication.meta[
                            LEGACY_POST_TASK_CALLBACK_ID_META_KEY
                        ].as_integer()
                        == task_id,
                    )
                )
                .order_by(Publication.id.asc())
                .limit(3)
            )
        ).all()
    )
    if not rows:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.LEGACY_ONLY)

    canonical_rows = []
    legacy_rows = []
    invalid = False
    for row in rows:
        mode = row.execution_mode
        if mode == CANONICAL_EXECUTION_MODE:
            canonical_rows.append(row)
            continue
        if (
            mode == INTENTIONAL_LEGACY_EXECUTION_MODE
            and row.legacy_post_task_id is not None
            and int(row.legacy_post_task_id) == task_id
        ):
            legacy_rows.append(row)
            continue
        invalid = True

    if invalid or legacy_rows and canonical_rows or len(canonical_rows) > 1:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    if not canonical_rows:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.LEGACY_ONLY)

    row = canonical_rows[0]
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
