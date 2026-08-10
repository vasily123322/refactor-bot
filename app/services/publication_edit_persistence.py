from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.content import LegacyPayloadError, document_from_legacy_payload
from app.services.telegram_results import normalize_telegram_message_ids


_EDITOR_NON_CONTENT_FIELDS = frozenset(
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


class PublicationEditPersistenceError(RuntimeError):
    pass


class PublicationEditConflictError(PublicationEditPersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class PublicationEditPersistenceResult:
    publication_id: int
    content_item_id: int
    previous_revision: int
    revision: int
    telegram_message_ids: tuple[int, ...]


def _content_payload(
    payload: Mapping[str, Any],
    *,
    runtime_option_keys: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    clean = deepcopy(dict(payload or {}))
    blocked = set(_EDITOR_NON_CONTENT_FIELDS)
    blocked.update(str(key) for key in runtime_option_keys if str(key))
    for key in blocked:
        clean.pop(key, None)
    for key in tuple(clean):
        if str(key).startswith("_"):
            clean.pop(key, None)
    return clean


class PublicationEditPersistenceService:
    """Persist a confirmed Telegram edit into canonical content state.

    This service must be called only after the provider edit is known to have
    succeeded. It intentionally does not talk to Telegram. Keeping that boundary
    explicit prevents a swallowed provider failure from creating a canonical revision
    that was never delivered.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def persist_success(
        self,
        *,
        publication_id: int,
        tg_user_id: int,
        expected_revision: int,
        payload: Mapping[str, Any],
        telegram_message_ids: list[int] | tuple[int, ...] | None = None,
    ) -> PublicationEditPersistenceResult:
        try:
            safe_publication_id = int(publication_id)
            safe_user_id = int(tg_user_id)
            safe_expected_revision = int(expected_revision)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PublicationEditPersistenceError("invalid canonical edit identity") from exc
        if safe_publication_id <= 0 or safe_user_id <= 0 or safe_expected_revision <= 0:
            raise PublicationEditPersistenceError("invalid canonical edit identity")
        if not isinstance(payload, Mapping):
            raise PublicationEditPersistenceError("editor payload must be an object")

        try:
            row = (
                await self.session.execute(
                    select(Publication, ScheduleEntry, ContentItem)
                    .join(Channel, Channel.id == Publication.channel_id)
                    .join(Client, Client.id == Channel.owner_id)
                    .join(ScheduleEntry, ScheduleEntry.id == Publication.schedule_entry_id)
                    .join(ContentItem, ContentItem.id == Publication.content_item_id)
                    .where(
                        Publication.id == safe_publication_id,
                        Client.tg_user_id == safe_user_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise PublicationEditPersistenceError("publication not found or not owned")

            publication, schedule, item = row
            publication_id_value = int(publication.id)
            content_item_id = int(item.id)
            publication_revision = int(publication.content_revision or 0)
            schedule_revision = int(schedule.content_revision or 0)
            current_revision = int(item.current_revision or 0)
            if str(publication.status or "") != "published":
                raise PublicationEditConflictError("publication is not published")
            if not (
                publication_revision == safe_expected_revision
                and schedule_revision == safe_expected_revision
                and current_revision == safe_expected_revision
            ):
                raise PublicationEditConflictError("canonical content revision changed")

            previous = (
                await self.session.execute(
                    select(ContentRevision).where(
                        ContentRevision.content_item_id == content_item_id,
                        ContentRevision.revision == safe_expected_revision,
                    )
                )
            ).scalar_one_or_none()
            if previous is None:
                raise PublicationEditConflictError("expected content revision is missing")

            runtime_options = dict(publication.meta or {}).get("runtime_options") or {}
            runtime_option_keys = (
                {str(key) for key in runtime_options}
                if isinstance(runtime_options, Mapping)
                else set()
            )
            try:
                document = document_from_legacy_payload(
                    _content_payload(payload, runtime_option_keys=runtime_option_keys)
                )
            except LegacyPayloadError as exc:
                raise PublicationEditPersistenceError(str(exc)) from exc

            if telegram_message_ids is None:
                ids = normalize_telegram_message_ids(publication.telegram_message_ids)
            else:
                ids = normalize_telegram_message_ids(list(telegram_message_ids))
            if not ids:
                raise PublicationEditPersistenceError(
                    "confirmed provider edit requires valid Telegram message ids"
                )

            next_revision = safe_expected_revision + 1
            revision = ContentRevision(
                content_item_id=content_item_id,
                revision=next_revision,
                document=document.to_dict(),
                source="telegram_edit",
                created_by_tg_user_id=safe_user_id,
                meta={
                    "publication_id": publication_id_value,
                    "edited_from_revision": safe_expected_revision,
                },
            )
            self.session.add(revision)
            item.current_revision = next_revision
            publication.content_revision = next_revision
            schedule.content_revision = next_revision
            publication.telegram_message_ids = ids

            await self.session.commit()
            return PublicationEditPersistenceResult(
                publication_id=publication_id_value,
                content_item_id=content_item_id,
                previous_revision=safe_expected_revision,
                revision=next_revision,
                telegram_message_ids=tuple(ids),
            )
        except Exception:
            await self.session.rollback()
            raise
