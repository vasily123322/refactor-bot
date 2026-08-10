from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentRevision
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.content import legacy_payload_from_document
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


_EDITOR_RUNTIME_BLOCKED = frozenset(
    {
        "_publication_id",
        "_content_item_id",
        "_content_revision",
        "_content_channel_id",
        "_post_task_id",
        "result_ids",
        "result_link",
        "primary_message_id",
        "notify_context",
        "repeat_on",
        "repeat_seconds",
        "repeat_group_id",
        "autodelete_at",
        "autodelete_effective_seconds",
        "autodeleted",
        "autodeleted_at",
        "autosign_applied",
    }
)


@dataclass(frozen=True, slots=True)
class PublicationEditorView:
    publication_id: int
    content_item_id: int
    content_revision: int
    channel_id: int
    channel_title: str
    tg_chat_id: int
    status: str
    scheduled_at: datetime
    timezone: str | None
    repeat_rule: dict[str, Any]
    publication_meta: dict[str, Any]
    document: dict[str, Any]
    telegram_message_ids: tuple[int, ...]
    result_link: str | None

    @property
    def primary_message_id(self) -> int | None:
        return self.telegram_message_ids[-1] if self.telegram_message_ids else None

    def editor_payload(self) -> dict[str, Any]:
        """Rebuild the legacy Telegram editor shape from canonical content state.

        This is an editor compatibility adapter only. Domain identity, delivery
        evidence and transport-generated runtime state remain canonical and are never
        copied back into the mutable editor payload, including from historical
        mirrored revisions that may still contain legacy extras.
        """
        payload = legacy_payload_from_document(self.document)
        for key in _EDITOR_RUNTIME_BLOCKED:
            payload.pop(key, None)
        for key in tuple(payload):
            if str(key).startswith("_"):
                payload.pop(key, None)

        runtime_options = self.publication_meta.get("runtime_options") or {}
        if isinstance(runtime_options, dict):
            for key, value in runtime_options.items():
                key_text = str(key)
                if (
                    key_text
                    and not key_text.startswith("_")
                    and key_text not in _EDITOR_RUNTIME_BLOCKED
                ):
                    payload.setdefault(key_text, deepcopy(value))

        if self.repeat_rule.get("enabled"):
            seconds = int(self.repeat_rule.get("seconds") or 0)
            if seconds > 0:
                payload["repeat_on"] = True
                payload["repeat_seconds"] = seconds
        elif self.repeat_rule:
            payload["repeat_on"] = False
            payload.pop("repeat_seconds", None)
        return payload


def publication_open_callback(publication_id: int, date_iso: str) -> str:
    return f"cp_open_pub:{int(publication_id)}:{date_iso}"


def publication_edit_callback(publication_id: int, date_iso: str) -> str:
    return f"cp_edit_pub:{int(publication_id)}:{date_iso}"


async def load_owned_publication_editor_view(
    session: AsyncSession,
    *,
    publication_id: int,
    tg_user_id: int,
) -> PublicationEditorView | None:
    """Load one canonical publication only when the Telegram user owns its channel.

    Missing, malformed and foreign-owner state intentionally collapse to ``None`` so
    callback callers cannot use the editor surface as an ownership oracle.
    """
    if int(publication_id) <= 0 or int(tg_user_id) <= 0:
        return None

    row = (
        await session.execute(
            select(Publication, ScheduleEntry, Channel)
            .join(Channel, Channel.id == Publication.channel_id)
            .join(Client, Client.id == Channel.owner_id)
            .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
            .where(
                Publication.id == int(publication_id),
                Client.tg_user_id == int(tg_user_id),
                ScheduleEntry.channel_id == Publication.channel_id,
                ScheduleEntry.content_item_id == Publication.content_item_id,
                ScheduleEntry.content_revision == Publication.content_revision,
            )
        )
    ).one_or_none()
    if row is None:
        return None

    publication, schedule, channel = row
    revision = (
        await session.execute(
            select(ContentRevision).where(
                ContentRevision.content_item_id == int(publication.content_item_id),
                ContentRevision.revision == int(publication.content_revision),
            )
        )
    ).scalar_one_or_none()
    if revision is None or not isinstance(revision.document, dict):
        return None

    ids = normalize_telegram_message_ids(publication.telegram_message_ids)
    return PublicationEditorView(
        publication_id=int(publication.id),
        content_item_id=int(publication.content_item_id),
        content_revision=int(publication.content_revision),
        channel_id=int(publication.channel_id),
        channel_title=str(channel.title or channel.tg_chat_id),
        tg_chat_id=int(channel.tg_chat_id),
        status=str(publication.status or "queued"),
        scheduled_at=schedule.scheduled_at,
        timezone=str(schedule.timezone) if schedule.timezone else None,
        repeat_rule=deepcopy(dict(schedule.repeat_rule or {})),
        publication_meta=deepcopy(dict(publication.meta or {})),
        document=deepcopy(dict(revision.document)),
        telegram_message_ids=tuple(ids),
        result_link=normalize_telegram_result_link(publication.result_link),
    )
