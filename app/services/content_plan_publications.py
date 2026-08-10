from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentRevision
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.content import legacy_payload_from_document


@dataclass(frozen=True, slots=True)
class OwnedPublicationContext:
    publication_id: int
    channel_id: int
    tg_chat_id: int
    channel_title: str | None
    status: str
    scheduled_at: datetime | None
    timezone_name: str | None
    result_link: str | None
    telegram_message_ids: tuple[int, ...]
    legacy_post_task_id: int | None
    publication_meta: dict[str, Any]
    schedule_meta: dict[str, Any]
    editor_payload: dict[str, Any]


def _editor_payload(document: PostDocument, *, runtime_options: dict[str, Any]) -> dict[str, Any]:
    if document.mode == "classic":
        payload = legacy_payload_from_document(document)
    else:
        # Rich documents are already the durable scheduler/editor representation.
        # Transport file IDs remain resolved only at the delivery edge.
        payload = {"type": "rich_document", "post_document": document.to_dict()}

    for key, value in runtime_options.items():
        if key not in payload and not str(key).startswith("_"):
            payload[str(key)] = deepcopy(value)
    return payload


async def load_owned_publication_context(
    session: AsyncSession,
    *,
    publication_id: int,
    tg_user_id: int,
) -> OwnedPublicationContext | None:
    """Load canonical publication/editor state only when the Telegram user owns it."""

    row = (
        await session.execute(
            select(Publication, Channel, ScheduleEntry)
            .join(Channel, Channel.id == Publication.channel_id)
            .join(Client, Client.id == Channel.owner_id)
            .outerjoin(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
            .where(
                Publication.id == int(publication_id),
                Client.tg_user_id == int(tg_user_id),
            )
        )
    ).one_or_none()
    if row is None:
        return None

    publication, channel, schedule = row
    revision = (
        await session.execute(
            select(ContentRevision).where(
                ContentRevision.content_item_id == int(publication.content_item_id),
                ContentRevision.revision == int(publication.content_revision),
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        return None

    document = PostDocument.from_dict(revision.document)
    publication_meta = deepcopy(dict(publication.meta or {}))
    schedule_meta = deepcopy(dict(schedule.meta or {})) if schedule is not None else {}
    runtime_options = publication_meta.get("runtime_options") or schedule_meta.get(
        "runtime_options"
    ) or {}
    if not isinstance(runtime_options, dict):
        runtime_options = {}

    message_ids: list[int] = []
    for raw in publication.telegram_message_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0:
            message_ids.append(value)

    return OwnedPublicationContext(
        publication_id=int(publication.id),
        channel_id=int(publication.channel_id),
        tg_chat_id=int(channel.tg_chat_id),
        channel_title=channel.title,
        status=str(publication.status or "queued"),
        scheduled_at=(schedule.scheduled_at if schedule is not None else None),
        timezone_name=(schedule.timezone if schedule is not None else None),
        result_link=publication.result_link,
        telegram_message_ids=tuple(message_ids),
        legacy_post_task_id=(
            int(publication.legacy_post_task_id)
            if publication.legacy_post_task_id is not None
            else None
        ),
        publication_meta=publication_meta,
        schedule_meta=schedule_meta,
        editor_payload=_editor_payload(document, runtime_options=runtime_options),
    )
