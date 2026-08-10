from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateError,
    PublicationAutodeleteViewStateService,
)


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewLegacySyncResult:
    scanned: int = 0
    synced: int = 0
    cleared: int = 0
    invalid: int = 0


def _payload(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


async def sync_active_legacy_view_intents(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> PublicationAutodeleteViewLegacySyncResult:
    """Stage current active PostTask view intent before Publication reconciliation.

    No commit is performed here. The caller can immediately run the existing bridge in
    the same session, making its commit include both the transport projection and this
    indexed scheduler state. Malformed view intent clears any stale indexed row so the
    future destructive evaluator cannot run from an obsolete threshold.
    """

    try:
        bounded_limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError, OverflowError):
        bounded_limit = 100

    rows = (
        await session.execute(
            select(Publication.id, PostTask.payload)
            .join(PostTask, PostTask.id == Publication.legacy_post_task_id)
            .where(Publication.status.in_(("queued", "sending")))
            .order_by(Publication.id.asc())
            .limit(bounded_limit)
        )
    ).all()

    service = PublicationAutodeleteViewStateService(session)
    synced = 0
    cleared = 0
    invalid = 0

    for publication_id, raw_payload in rows:
        payload = _payload(raw_payload)
        raw_threshold = payload.get("autodelete_views") if payload is not None else None
        try:
            snapshot = await service.sync_intent(
                publication_id=int(publication_id),
                threshold=raw_threshold,
            )
        except PublicationAutodeleteViewStateError:
            invalid += 1
            await service.sync_intent(
                publication_id=int(publication_id),
                threshold=None,
            )
            cleared += 1
            continue

        if snapshot is None:
            cleared += 1
        else:
            synced += 1

    return PublicationAutodeleteViewLegacySyncResult(
        scanned=len(rows),
        synced=synced,
        cleared=cleared,
        invalid=invalid,
    )
