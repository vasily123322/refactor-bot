from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.content import LegacyPayloadError, document_from_legacy_payload
from app.services.publication_edit_autodelete import (
    PublicationEditAutodeleteSyncError,
    PublicationEditAutodeleteSyncService,
)
from app.services.telegram_results import normalize_telegram_message_ids


_EDITOR_RUNTIME_OPTION_FIELDS = frozenset(
    {
        "autodelete_seconds",
        "autodelete_views",
        "autodelete_report",
    }
)
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
        "autodelete_label",
        "autodeleted",
        "autodeleted_at",
        "autosign_applied",
        *_EDITOR_RUNTIME_OPTION_FIELDS,
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


def _positive_editor_int(value: Any, *, field: str) -> int | None:
    if value in (None, "", 0, "0", False):
        return None
    if isinstance(value, bool):
        raise PublicationEditPersistenceError(f"invalid canonical runtime option: {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PublicationEditPersistenceError(
            f"invalid canonical runtime option: {field}"
        ) from exc
    if parsed <= 0:
        raise PublicationEditPersistenceError(f"invalid canonical runtime option: {field}")
    return parsed


def _edited_runtime_options(
    existing: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge mutable editor intent without treating it as immutable post content."""
    if existing is None:
        current: dict[str, Any] = {}
    elif isinstance(existing, Mapping):
        current = deepcopy(dict(existing))
    else:
        raise PublicationEditPersistenceError("canonical runtime options are malformed")

    for key in _EDITOR_RUNTIME_OPTION_FIELDS:
        current.pop(key, None)

    seconds = _positive_editor_int(
        payload.get("autodelete_seconds"), field="autodelete_seconds"
    )
    views = _positive_editor_int(
        payload.get("autodelete_views"), field="autodelete_views"
    )
    if seconds is not None and views is not None:
        raise PublicationEditPersistenceError(
            "canonical autodelete timer and views are mutually exclusive"
        )
    if seconds is not None:
        current["autodelete_seconds"] = seconds
    if views is not None:
        current["autodelete_views"] = views

    report = payload.get("autodelete_report")
    if report is not None and not isinstance(report, bool):
        raise PublicationEditPersistenceError(
            "invalid canonical runtime option: autodelete_report"
        )
    if report is True:
        current["autodelete_report"] = True

    return current


def validate_publication_edit_runtime_options(
    existing: Any,
    payload: Mapping[str, Any],
) -> None:
    """Fail before provider side effects when editor runtime intent is invalid."""
    if not isinstance(payload, Mapping):
        raise PublicationEditPersistenceError("editor payload must be an object")
    _edited_runtime_options(existing, payload)


def edited_publication_runtime_options(
    existing: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the normalized editor runtime intent for preflight/execution guards."""
    if not isinstance(payload, Mapping):
        raise PublicationEditPersistenceError("editor payload must be an object")
    return _edited_runtime_options(existing, payload)


def _with_runtime_options(meta: Any, runtime_options: Mapping[str, Any]) -> dict[str, Any]:
    if meta is None:
        result: dict[str, Any] = {}
    elif isinstance(meta, Mapping):
        result = deepcopy(dict(meta))
    else:
        raise PublicationEditPersistenceError("canonical publication metadata is malformed")
    if runtime_options:
        result["runtime_options"] = deepcopy(dict(runtime_options))
    else:
        result.pop("runtime_options", None)
    return result


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
        now: datetime | None = None,
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
            if str(publication.status or "") != "published" or str(
                schedule.status or ""
            ) != "completed":
                raise PublicationEditConflictError(
                    "publication lifecycle is not consistently published"
                )
            if str(item.kind or "") != "post":
                raise PublicationEditConflictError("content item is not an editable post")
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

            publication_meta = dict(publication.meta or {})
            existing_runtime_options = publication_meta.get("runtime_options")
            runtime_options = _edited_runtime_options(existing_runtime_options, payload)
            existing_runtime_option_keys = (
                {str(key) for key in existing_runtime_options}
                if isinstance(existing_runtime_options, Mapping)
                else set()
            )
            try:
                document = document_from_legacy_payload(
                    _content_payload(
                        payload,
                        runtime_option_keys=(
                            existing_runtime_option_keys
                            | set(_EDITOR_RUNTIME_OPTION_FIELDS)
                        ),
                    )
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

            try:
                await PublicationEditAutodeleteSyncService(self.session).apply(
                    publication=publication,
                    schedule=schedule,
                    previous_runtime_options=(
                        dict(existing_runtime_options)
                        if isinstance(existing_runtime_options, Mapping)
                        else {}
                    ),
                    runtime_options=runtime_options,
                    now=now,
                )
            except PublicationEditAutodeleteSyncError as exc:
                raise PublicationEditConflictError(str(exc)) from None

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
            publication.meta = _with_runtime_options(publication.meta, runtime_options)
            schedule.meta = _with_runtime_options(schedule.meta, runtime_options)

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
