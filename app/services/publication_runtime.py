from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication


AUTODELETE_RUNTIME_META_KEY = "autodelete_runtime"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _utc_iso(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 128:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def normalize_autodelete_runtime(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Extract generated autodelete lifecycle without trusting raw legacy payload."""
    data = dict(payload or {})
    effective_seconds = _positive_int(
        data.get("autodelete_effective_seconds") or data.get("autodelete_seconds")
    )
    scheduled_at = _utc_iso(data.get("autodelete_at"))
    deleted = data.get("autodeleted") is True
    deleted_at = _utc_iso(data.get("autodeleted_at")) if deleted else None

    # No runtime evidence means no canonical generated state yet. Queue-time intent
    # remains separately preserved in meta.runtime_options from #85.
    if effective_seconds is None and scheduled_at is None and not deleted:
        return None

    state: dict[str, Any] = {"deleted": deleted}
    if effective_seconds is not None:
        state["effective_seconds"] = effective_seconds
    if scheduled_at is not None:
        state["scheduled_at"] = scheduled_at
    if deleted_at is not None:
        state["deleted_at"] = deleted_at
    return state


class PublicationRuntimeProjector:
    """Mirror generated PostTask runtime facts into the canonical Publication row."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def project_task(self, task_id: int, payload: Mapping[str, Any] | None) -> bool:
        publication = (
            await self.session.execute(
                select(Publication).where(
                    Publication.legacy_post_task_id == int(task_id)
                )
            )
        ).scalar_one_or_none()
        if publication is None:
            return False

        meta = deepcopy(dict(publication.meta or {}))
        state = normalize_autodelete_runtime(payload)
        if state is None:
            meta.pop(AUTODELETE_RUNTIME_META_KEY, None)
        else:
            meta[AUTODELETE_RUNTIME_META_KEY] = state

        if meta == dict(publication.meta or {}):
            return False
        publication.meta = meta
        await self.session.commit()
        return True
