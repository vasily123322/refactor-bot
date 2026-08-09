from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publishing.models import Publication


AUTODELETE_RUNTIME_META_KEY = "autodelete_runtime"
_TERMINAL_PUBLICATION_STATUSES = frozenset(
    {"published", "failed", "skipped", "cancelled"}
)


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


@dataclass(frozen=True, slots=True)
class PublicationRuntimeBackfillBatch:
    scanned: int
    updated: int
    next_cursor: int
    done: bool


class PublicationRuntimeProjector:
    """Mirror generated PostTask runtime facts into the canonical Publication row."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _apply_runtime(
        publication: Publication,
        payload: Mapping[str, Any] | None,
    ) -> bool:
        original = dict(publication.meta or {})
        meta = deepcopy(original)
        state = normalize_autodelete_runtime(payload)
        if state is None:
            meta.pop(AUTODELETE_RUNTIME_META_KEY, None)
        else:
            meta[AUTODELETE_RUNTIME_META_KEY] = state

        if meta == original:
            return False
        publication.meta = meta
        return True

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

        if not self._apply_runtime(publication, payload):
            return False
        await self.session.commit()
        return True

    async def backfill_terminal(
        self,
        *,
        after_publication_id: int = 0,
        limit: int = 100,
    ) -> PublicationRuntimeBackfillBatch:
        """Scan a bounded historical terminal slice once per process.

        New scheduler paths already project runtime synchronously. This cursor-based
        pass exists only for Publications that reached a terminal state before runtime
        projection was introduced. It avoids JSON-dialect filtering and never scans
        more than `limit` rows in one reconciler tick.
        """
        bounded_limit = max(1, min(int(limit), 500))
        cursor = max(0, int(after_publication_id))
        rows = (
            await self.session.execute(
                select(Publication, PostTask.payload)
                .join(
                    PostTask,
                    PostTask.id == Publication.legacy_post_task_id,
                )
                .where(
                    Publication.id > cursor,
                    Publication.status.in_(tuple(_TERMINAL_PUBLICATION_STATUSES)),
                )
                .order_by(Publication.id.asc())
                .limit(bounded_limit)
            )
        ).all()

        updated = 0
        next_cursor = cursor
        for publication, payload in rows:
            next_cursor = max(next_cursor, int(publication.id))
            if self._apply_runtime(publication, payload):
                updated += 1

        if updated:
            await self.session.commit()

        return PublicationRuntimeBackfillBatch(
            scanned=len(rows),
            updated=updated,
            next_cursor=next_cursor,
            done=len(rows) < bounded_limit,
        )
