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


def _legacy_time_intent(payload: Mapping[str, Any] | None) -> tuple[bool, bool]:
    """Return (active, invalid) for legacy timer fields used by mixed fallback."""

    if payload is None:
        return False, False

    active = False
    invalid = False
    for key in ("autodelete_effective_seconds", "autodelete_seconds"):
        raw = payload.get(key)
        if raw is None or raw is False or raw == 0 or raw == "0" or raw == "":
            continue
        if isinstance(raw, bool):
            invalid = True
            continue
        try:
            seconds = int(raw)
        except (TypeError, ValueError, OverflowError):
            invalid = True
            continue
        if seconds > 0:
            active = True
        elif seconds < 0:
            invalid = True
    return active, invalid


async def sync_active_legacy_view_intents(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> PublicationAutodeleteViewLegacySyncResult:
    """Stage current active PostTask view intent before Publication reconciliation.

    No commit is performed here. The caller can immediately run the existing bridge in
    the same session, making its commit include both the transport projection and this
    indexed scheduler state. Mixed time+views remains legacy-owned and is deliberately
    excluded from the canonical views index. Malformed view or timer intent clears any
    stale indexed row so the destructive evaluator cannot run from obsolete provenance.
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
        has_time_intent, invalid_time_intent = _legacy_time_intent(payload)
        if invalid_time_intent:
            invalid += 1
            await service.sync_intent(
                publication_id=int(publication_id),
                threshold=None,
            )
            cleared += 1
            continue

        raw_threshold = (
            None
            if has_time_intent
            else (payload.get("autodelete_views") if payload is not None else None)
        )
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