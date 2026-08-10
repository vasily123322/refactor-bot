from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument, PostDocumentError
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.content import (
    LegacyPayloadError,
    document_from_legacy_payload,
    legacy_payload_from_document,
)
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


@dataclass(frozen=True, slots=True)
class PublicationEditorSaveResult:
    publication_id: int
    content_item_id: int
    previous_revision: int
    content_revision: int
    telegram_message_ids: tuple[int, ...]
    result_link: str | None


def publication_open_callback(publication_id: int, date_iso: str) -> str:
    return f"cp_open_pub:{int(publication_id)}:{date_iso}"


def publication_edit_callback(publication_id: int, date_iso: str) -> str:
    return f"cp_edit_pub:{int(publication_id)}:{date_iso}"


def _edited_document(
    base_document: Mapping[str, Any], editor_payload: Mapping[str, Any]
) -> PostDocument:
    """Build a new classic document while preserving provenance, not runtime residue."""
    base = PostDocument.from_dict(base_document)
    edited = document_from_legacy_payload(editor_payload)
    if not edited.blocks or edited.blocks[0].get("type") == "legacy":
        raise LegacyPayloadError("opaque editor payload cannot become canonical content")

    metadata = deepcopy(dict(base.metadata or {}))
    # A new canonical editor write must not perpetuate historical scheduler/transport
    # extras. Other provenance metadata remains attached to the content lineage.
    metadata.pop("legacy_payload_extra", None)
    if edited.metadata.get("legacy_type"):
        metadata["legacy_type"] = str(edited.metadata["legacy_type"])
    metadata.pop("opaque_legacy", None)

    return PostDocument(
        mode=edited.mode,
        blocks=deepcopy(edited.blocks),
        telegram=deepcopy(edited.telegram),
        metadata=metadata,
    )


def _safe_primary_message_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        message_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return message_id if message_id > 0 else None


def _updated_delivery_identity(
    publication: Publication, primary_message_id: int | None
) -> tuple[list[int], str | None]:
    ids = normalize_telegram_message_ids(publication.telegram_message_ids)
    link = normalize_telegram_result_link(publication.result_link)
    if primary_message_id is None:
        return ids, link

    ids = [message_id for message_id in ids if message_id != primary_message_id]
    ids.append(primary_message_id)
    if link is not None:
        link = f"{link.rsplit('/', 1)[0]}/{primary_message_id}"
    return ids, link


async def save_owned_publication_editor_revision(
    session: AsyncSession,
    *,
    publication_id: int,
    tg_user_id: int,
    expected_content_item_id: int,
    expected_content_revision: int,
    editor_payload: Mapping[str, Any],
    primary_message_id: int | None = None,
) -> PublicationEditorSaveResult | None:
    """Persist one successful Telegram edit back into canonical content state.

    The save is intentionally optimistic: the exact ContentItem/revision observed when
    the editor opened must still be current. Ownership and all Publication/Schedule/
    ContentItem identity seams are rechecked under row locks. Stale, malformed or
    foreign state fails closed with ``None`` and never creates a partial revision.
    """
    try:
        safe_publication_id = int(publication_id)
        safe_user_id = int(tg_user_id)
        safe_item_id = int(expected_content_item_id)
        safe_revision = int(expected_content_revision)
    except (TypeError, ValueError, OverflowError):
        return None
    if min(safe_publication_id, safe_user_id, safe_item_id, safe_revision) <= 0:
        return None
    if not isinstance(editor_payload, Mapping):
        return None

    safe_primary = _safe_primary_message_id(primary_message_id)
    if primary_message_id is not None and safe_primary is None:
        return None

    try:
        row = (
            await session.execute(
                select(Publication, ScheduleEntry, ContentItem)
                .join(Channel, Channel.id == Publication.channel_id)
                .join(Client, Client.id == Channel.owner_id)
                .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
                .join(ContentItem, ContentItem.id == Publication.content_item_id)
                .where(
                    Publication.id == safe_publication_id,
                    Client.tg_user_id == safe_user_id,
                    Publication.status == "published",
                    Publication.content_item_id == safe_item_id,
                    Publication.content_revision == safe_revision,
                    ScheduleEntry.channel_id == Publication.channel_id,
                    ScheduleEntry.content_item_id == Publication.content_item_id,
                    ScheduleEntry.content_revision == Publication.content_revision,
                    ContentItem.channel_id == Publication.channel_id,
                    ContentItem.id == safe_item_id,
                    ContentItem.current_revision == safe_revision,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            await session.rollback()
            return None
        publication, schedule, item = row

        base_revision = (
            await session.execute(
                select(ContentRevision)
                .where(
                    ContentRevision.content_item_id == safe_item_id,
                    ContentRevision.revision == safe_revision,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if base_revision is None or not isinstance(base_revision.document, dict):
            await session.rollback()
            return None

        try:
            document = _edited_document(base_revision.document, editor_payload)
        except (LegacyPayloadError, PostDocumentError, TypeError, ValueError):
            await session.rollback()
            return None

        next_revision = safe_revision + 1
        revision = ContentRevision(
            content_item_id=safe_item_id,
            revision=next_revision,
            document=document.to_dict(),
            source="publication_editor",
            created_by_tg_user_id=safe_user_id,
            meta={
                "publication_id": safe_publication_id,
                "edited_from_revision": safe_revision,
            },
        )
        session.add(revision)
        item.current_revision = next_revision
        publication.content_revision = next_revision
        schedule.content_revision = next_revision

        ids, result_link = _updated_delivery_identity(publication, safe_primary)
        publication.telegram_message_ids = list(ids)
        publication.result_link = result_link
        publication_meta = deepcopy(dict(publication.meta or {}))
        publication_meta["canonical_edit"] = {
            "revision": next_revision,
            "edited_at": datetime.now(timezone.utc).isoformat(),
        }
        publication.meta = publication_meta

        await session.commit()
        return PublicationEditorSaveResult(
            publication_id=safe_publication_id,
            content_item_id=safe_item_id,
            previous_revision=safe_revision,
            content_revision=next_revision,
            telegram_message_ids=tuple(ids),
            result_link=result_link,
        )
    except Exception:
        await session.rollback()
        raise


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
    try:
        safe_publication_id = int(publication_id)
        safe_user_id = int(tg_user_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if safe_publication_id <= 0 or safe_user_id <= 0:
        return None

    row = (
        await session.execute(
            select(Publication, ScheduleEntry, Channel)
            .join(Channel, Channel.id == Publication.channel_id)
            .join(Client, Client.id == Channel.owner_id)
            .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
            .where(
                Publication.id == safe_publication_id,
                Client.tg_user_id == safe_user_id,
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
            select(ContentRevision)
            .join(ContentItem, ContentItem.id == ContentRevision.content_item_id)
            .where(
                ContentRevision.content_item_id == int(publication.content_item_id),
                ContentRevision.revision == int(publication.content_revision),
                ContentItem.channel_id == int(publication.channel_id),
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
