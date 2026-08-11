from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.scheduling import as_utc


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _json_snapshot(value: Any) -> str | None:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _runtime_options(
    publication_meta: Mapping[str, Any],
    schedule_meta: Mapping[str, Any],
) -> dict[str, Any] | None:
    publication_value = publication_meta.get("runtime_options")
    schedule_value = schedule_meta.get("runtime_options")
    if publication_value is None and schedule_value is None:
        return {}
    publication_options = _mapping(publication_value)
    schedule_options = _mapping(schedule_value)
    if publication_options is None or schedule_options is None:
        return None
    if publication_options != schedule_options:
        return None
    return publication_options


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryPlan:
    publication_id: int
    schedule_entry_id: int
    channel_id: int
    telegram_chat_id: int
    content_item_id: int
    content_revision: int
    scheduled_at: datetime
    timezone: str | None
    document_snapshot: str
    runtime_options_snapshot: str

    def post_document(self) -> PostDocument:
        raw = json.loads(self.document_snapshot)
        if not isinstance(raw, dict):
            raise ValueError("canonical delivery document snapshot is not an object")
        return PostDocument.from_dict(raw)

    def runtime_options(self) -> dict[str, Any]:
        raw = json.loads(self.runtime_options_snapshot)
        if not isinstance(raw, dict):
            raise ValueError("canonical delivery runtime snapshot is not an object")
        return raw


class CanonicalPublicationDeliveryPlanner:
    """Pure/read-only proof for one due canonical Publication delivery.

    The planner reads only canonical publication/content/channel state. Linked
    ``PostTask`` transport identity is intentionally ignored so a publication remains
    executable after compatibility transport retirement. Delivery side effects, leases,
    attempt creation and status transitions are later migration stages.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        publication_id: int,
        *,
        at: datetime | None = None,
    ) -> CanonicalPublicationDeliveryPlan | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        current = as_utc(at or datetime.now(timezone.utc))
        source = (
            await self.session.execute(
                select(
                    Publication,
                    ScheduleEntry,
                    ContentItem,
                    ContentRevision,
                    Channel,
                )
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .join(
                    ContentItem,
                    and_(
                        ContentItem.id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                        ContentItem.kind == "post",
                    ),
                )
                .join(
                    ContentRevision,
                    and_(
                        ContentRevision.content_item_id == Publication.content_item_id,
                        ContentRevision.revision == Publication.content_revision,
                    ),
                )
                .join(
                    Channel,
                    and_(
                        Channel.id == Publication.channel_id,
                        Channel.is_active.is_(True),
                    ),
                )
                .where(
                    Publication.id == safe_publication_id,
                    Publication.status == "queued",
                    ScheduleEntry.status == "pending",
                    ScheduleEntry.scheduled_at <= current,
                )
            )
        ).one_or_none()
        if source is None:
            return None
        publication, schedule, item, revision, channel = source

        if (
            int(publication.attempt_count or 0) != 0
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
        ):
            return None

        attempt_id = (
            await self.session.execute(
                select(PublicationAttempt.id)
                .where(PublicationAttempt.publication_id == int(publication.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt_id is not None:
            return None

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        if publication_meta is None or schedule_meta is None:
            return None
        runtime_options = _runtime_options(publication_meta, schedule_meta)
        if runtime_options is None:
            return None

        try:
            document = PostDocument.from_dict(revision.document)
        except (TypeError, ValueError):
            return None
        document_snapshot = _json_snapshot(document.to_dict())
        runtime_snapshot = _json_snapshot(runtime_options)
        if document_snapshot is None or runtime_snapshot is None:
            return None

        return CanonicalPublicationDeliveryPlan(
            publication_id=int(publication.id),
            schedule_entry_id=int(schedule.id),
            channel_id=int(channel.id),
            telegram_chat_id=int(channel.tg_chat_id),
            content_item_id=int(item.id),
            content_revision=int(revision.revision),
            scheduled_at=as_utc(schedule.scheduled_at),
            timezone=schedule.timezone,
            document_snapshot=document_snapshot,
            runtime_options_snapshot=runtime_snapshot,
        )
