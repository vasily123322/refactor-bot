from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry


MAX_RECENT_CONTEXT_ITEMS = 5
MAX_SCHEDULED_CONTEXT_ITEMS = 3
MAX_CONTEXT_ITEMS = 8
MAX_CONTEXT_EXCERPT_CHARS = 360
MAX_CONTEXT_TOTAL_CHARS = 2400


def _bounded_text(value: str, limit: int) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


def _document_excerpt(document: Any, limit: int) -> str:
    if not isinstance(document, dict):
        return ""
    chunks: list[str] = []
    for block in document.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for key in ("text", "caption"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                chunks.append(value)
        if sum(len(chunk) for chunk in chunks) >= limit:
            break
    return _bounded_text(" ".join(chunks), limit)


@dataclass(frozen=True, slots=True)
class EditorialContextSnapshot:
    items: tuple[dict[str, Any], ...]
    recent_count: int
    scheduled_count: int
    fingerprint: str
    total_excerpt_chars: int

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "items": [dict(item) for item in self.items],
            "bounds": {
                "max_items": MAX_CONTEXT_ITEMS,
                "max_excerpt_chars": MAX_CONTEXT_EXCERPT_CHARS,
                "max_total_chars": MAX_CONTEXT_TOTAL_CHARS,
            },
        }

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "recent_count": self.recent_count,
            "scheduled_count": self.scheduled_count,
            "item_count": len(self.items),
            "total_excerpt_chars": self.total_excerpt_chars,
            "fingerprint": self.fingerprint,
            "refs": [
                {
                    key: int(item[key])
                    for key in (
                        "content_item_id",
                        "revision",
                        "schedule_entry_id",
                        "publication_id",
                    )
                    if item.get(key) is not None
                }
                for item in self.items
            ],
        }


class EditorialContextService:
    """Bounded, channel-scoped editorial context over canonical Content data only."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def snapshot(
        self,
        *,
        channel_id: int,
        now_utc: datetime | None = None,
    ) -> EditorialContextSnapshot:
        channel_id = int(channel_id)
        now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        items: list[dict[str, Any]] = []
        total_chars = 0
        recent_count = 0
        scheduled_count = 0

        recent_rows = (
            await self.session.execute(
                select(ContentItem, ContentRevision)
                .join(
                    ContentRevision,
                    and_(
                        ContentRevision.content_item_id == ContentItem.id,
                        ContentRevision.revision == ContentItem.current_revision,
                    ),
                )
                .where(ContentItem.channel_id == channel_id)
                .order_by(ContentItem.updated_at.desc(), ContentItem.id.desc())
                .limit(MAX_RECENT_CONTEXT_ITEMS)
            )
        ).all()

        def append_item(
            *,
            kind: str,
            item: ContentItem,
            revision: ContentRevision,
            schedule_entry_id: int | None = None,
            publication_id: int | None = None,
            scheduled_at: datetime | None = None,
        ) -> bool:
            nonlocal total_chars
            if len(items) >= MAX_CONTEXT_ITEMS:
                return False
            remaining = MAX_CONTEXT_TOTAL_CHARS - total_chars
            if remaining <= 0:
                return False
            excerpt_limit = min(MAX_CONTEXT_EXCERPT_CHARS, remaining)
            excerpt = _document_excerpt(revision.document, excerpt_limit)
            title = _bounded_text(str(item.title or ""), 180)
            if not title and not excerpt:
                return False
            payload: dict[str, Any] = {
                "kind": kind,
                "content_item_id": int(item.id),
                "revision": int(revision.revision),
                "title": title,
                "excerpt": excerpt,
            }
            if schedule_entry_id is not None:
                payload["schedule_entry_id"] = int(schedule_entry_id)
            if publication_id is not None:
                payload["publication_id"] = int(publication_id)
            if scheduled_at is not None:
                value = scheduled_at
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                payload["scheduled_at"] = value.astimezone(timezone.utc).isoformat()
            items.append(payload)
            total_chars += len(excerpt)
            return True

        for item, revision in recent_rows:
            if append_item(kind="recent_content", item=item, revision=revision):
                recent_count += 1

        scheduled_rows = (
            await self.session.execute(
                select(ScheduleEntry, ContentItem, ContentRevision, Publication)
                .join(ContentItem, ContentItem.id == ScheduleEntry.content_item_id)
                .join(
                    ContentRevision,
                    and_(
                        ContentRevision.content_item_id == ScheduleEntry.content_item_id,
                        ContentRevision.revision == ScheduleEntry.content_revision,
                    ),
                )
                .outerjoin(Publication, Publication.schedule_entry_id == ScheduleEntry.id)
                .where(
                    ScheduleEntry.channel_id == channel_id,
                    ScheduleEntry.status == "pending",
                    ScheduleEntry.scheduled_at >= now,
                )
                .order_by(ScheduleEntry.scheduled_at.asc(), ScheduleEntry.id.asc())
                .limit(MAX_SCHEDULED_CONTEXT_ITEMS)
            )
        ).all()
        for schedule, item, revision, publication in scheduled_rows:
            if append_item(
                kind="scheduled_content",
                item=item,
                revision=revision,
                schedule_entry_id=int(schedule.id),
                publication_id=(int(publication.id) if publication is not None else None),
                scheduled_at=schedule.scheduled_at,
            ):
                scheduled_count += 1

        canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return EditorialContextSnapshot(
            items=tuple(items),
            recent_count=recent_count,
            scheduled_count=scheduled_count,
            fingerprint=fingerprint,
            total_excerpt_chars=total_chars,
        )
