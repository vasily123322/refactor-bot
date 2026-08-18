from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.content import LegacyPayloadError, document_from_legacy_payload
from app.services.publication_bridge import _runtime_intent, _scheduler_payload
from app.services.publication_edit_persistence import (
    PublicationEditConflictError,
    PublicationEditPersistenceError,
    _content_payload,
)
from app.services.rich_media_assets import RichMediaAssetError, RichMediaAssetResolver
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class QueuedCanonicalPublicationEditResult:
    publication_id: int
    content_item_id: int
    previous_revision: int
    revision: int


class QueuedCanonicalPublicationEditCoordinator:
    """Atomically edit canonical content before delivery has started.

    Publication identity and canonical content are authoritative. The linked PostTask is
    only a compatibility projection for the legacy scheduler and is updated in the same
    transaction after all ownership, lifecycle, linkage and execution barriers pass.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _safe_identity(
        *,
        publication_id: int,
        tg_user_id: int,
        expected_revision: int,
    ) -> tuple[int, int, int]:
        try:
            safe_publication_id = int(publication_id)
            safe_user_id = int(tg_user_id)
            safe_expected_revision = int(expected_revision)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PublicationEditPersistenceError("invalid canonical edit identity") from exc
        if (
            safe_publication_id <= 0
            or safe_user_id <= 0
            or safe_expected_revision <= 0
        ):
            raise PublicationEditPersistenceError("invalid canonical edit identity")
        return safe_publication_id, safe_user_id, safe_expected_revision

    @staticmethod
    def _canonical_runtime_options(publication: Publication) -> dict[str, Any]:
        raw = dict(publication.meta or {}).get("runtime_options")
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise PublicationEditConflictError(
                "canonical publication runtime options are malformed"
            )
        return deepcopy(dict(raw))

    async def _compatibility_payload(
        self,
        session: AsyncSession,
        *,
        document,
        channel_id: int,
        repeat_rule: Mapping[str, Any],
        runtime_options: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            render_document = await RichMediaAssetResolver(session).resolve(
                document,
                channel_id=int(channel_id),
            )
            projected = _scheduler_payload(document, render_document=render_document)
        except (RichMediaAssetError, RuntimeError) as exc:
            raise PublicationEditPersistenceError(str(exc)) from exc

        rule = deepcopy(dict(repeat_rule or {}))
        if rule.get("enabled"):
            try:
                seconds = int(rule.get("seconds") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise PublicationEditConflictError(
                    "canonical repeat rule is malformed"
                ) from exc
            if seconds <= 0:
                raise PublicationEditConflictError("canonical repeat rule is malformed")
            projected["repeat_on"] = True
            projected["repeat_seconds"] = seconds
        elif rule:
            projected["repeat_on"] = False
            projected.pop("repeat_seconds", None)

        try:
            runtime_intent = _runtime_intent(runtime_options, payload=projected)
        except RuntimeError as exc:
            raise PublicationEditConflictError(str(exc)) from exc
        for key, value in runtime_intent.items():
            projected[key] = deepcopy(value)
        return projected

    async def edit_and_persist(
        self,
        *,
        publication_id: int,
        tg_user_id: int,
        expected_revision: int,
        payload: Mapping[str, Any],
        now: datetime | None = None,
    ) -> QueuedCanonicalPublicationEditResult:
        safe_publication_id, safe_user_id, safe_expected_revision = self._safe_identity(
            publication_id=publication_id,
            tg_user_id=tg_user_id,
            expected_revision=expected_revision,
        )
        if not isinstance(payload, Mapping):
            raise PublicationEditPersistenceError("editor payload must be an object")
        safe_now = now or datetime.now(timezone.utc)

        async with self.session_factory() as session:
            try:
                publication = (
                    await session.execute(
                        select(Publication)
                        .join(Channel, Channel.id == Publication.channel_id)
                        .join(Client, Client.id == Channel.owner_id)
                        .where(
                            Publication.id == safe_publication_id,
                            Client.tg_user_id == safe_user_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if publication is None:
                    raise PublicationEditPersistenceError(
                        "publication not found or not owned"
                    )
                if str(publication.status or "") != "queued":
                    raise PublicationEditConflictError(
                        "publication lifecycle is not safely queued"
                    )
                if int(publication.attempt_count or 0) != 0:
                    raise PublicationEditConflictError(
                        "publication execution has already started"
                    )
                if publication.telegram_message_ids or publication.result_link:
                    raise PublicationEditConflictError(
                        "queued publication contains delivery evidence"
                    )

                schedule_entry_id = int(publication.schedule_entry_id or 0)
                if schedule_entry_id <= 0:
                    raise PublicationEditConflictError(
                        "queued publication schedule linkage is missing"
                    )
                schedule = (
                    await session.execute(
                        select(ScheduleEntry)
                        .where(ScheduleEntry.id == schedule_entry_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if schedule is None:
                    raise PublicationEditConflictError(
                        "queued publication schedule linkage is missing"
                    )

                item = (
                    await session.execute(
                        select(ContentItem)
                        .where(ContentItem.id == int(publication.content_item_id))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if item is None:
                    raise PublicationEditConflictError(
                        "queued publication content linkage is missing"
                    )

                publication_revision = int(publication.content_revision or 0)
                schedule_revision = int(schedule.content_revision or 0)
                current_revision = int(item.current_revision or 0)
                if (
                    str(schedule.status or "") != "pending"
                    or str(item.kind or "") != "post"
                    or int(schedule.channel_id) != int(publication.channel_id)
                    or int(schedule.content_item_id) != int(publication.content_item_id)
                    or int(item.channel_id) != int(publication.channel_id)
                    or publication_revision != safe_expected_revision
                    or schedule_revision != safe_expected_revision
                    or current_revision != safe_expected_revision
                ):
                    raise PublicationEditConflictError(
                        "queued canonical publication linkage changed"
                    )

                previous = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id == int(item.id),
                            ContentRevision.revision == safe_expected_revision,
                        )
                    )
                ).scalar_one_or_none()
                if previous is None or not isinstance(previous.document, dict):
                    raise PublicationEditConflictError(
                        "expected canonical content revision is missing"
                    )

                started_attempt = (
                    await session.execute(
                        select(PublicationAttempt.id)
                        .where(PublicationAttempt.publication_id == safe_publication_id)
                        .limit(1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if started_attempt is not None:
                    raise PublicationEditConflictError(
                        "publication execution attempt already exists"
                    )

                active_lease = (
                    await session.execute(
                        select(PublicationDeliveryLease.publication_id)
                        .where(
                            PublicationDeliveryLease.publication_id
                            == safe_publication_id,
                            PublicationDeliveryLease.expires_at > safe_now,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if active_lease is not None:
                    raise PublicationEditConflictError(
                        "publication delivery lease is active"
                    )

                legacy_task_id = int(publication.legacy_post_task_id or 0)
                if legacy_task_id <= 0:
                    raise PublicationEditConflictError(
                        "queued compatibility transport linkage is missing"
                    )
                try:
                    schedule_task_id = int(
                        dict(schedule.meta or {}).get("legacy_post_task_id") or 0
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    raise PublicationEditConflictError(
                        "queued compatibility transport linkage is malformed"
                    ) from exc
                if schedule_task_id != legacy_task_id:
                    raise PublicationEditConflictError(
                        "queued compatibility transport linkage changed"
                    )

                task = (
                    await session.execute(
                        select(PostTask)
                        .where(PostTask.id == legacy_task_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if task is None:
                    raise PublicationEditConflictError(
                        "queued compatibility transport is missing"
                    )
                if (
                    str(task.status or "") != "pending"
                    or int(task.channel_id) != int(publication.channel_id)
                    or str(task.dedupe_key or "")
                    != f"publication:{safe_publication_id}"
                    or as_utc(task.scheduled_at) != as_utc(schedule.scheduled_at)
                ):
                    raise PublicationEditConflictError(
                        "queued compatibility transport is not safely pending"
                    )

                runtime_options = self._canonical_runtime_options(publication)
                try:
                    document = document_from_legacy_payload(
                        _content_payload(
                            payload,
                            runtime_option_keys={str(key) for key in runtime_options},
                        )
                    )
                except LegacyPayloadError as exc:
                    raise PublicationEditPersistenceError(str(exc)) from exc

                projected_payload = await self._compatibility_payload(
                    session,
                    document=document,
                    channel_id=int(publication.channel_id),
                    repeat_rule=dict(schedule.repeat_rule or {}),
                    runtime_options=runtime_options,
                )

                next_revision = safe_expected_revision + 1
                session.add(
                    ContentRevision(
                        content_item_id=int(item.id),
                        revision=next_revision,
                        document=document.to_dict(),
                        source="queued_canonical_edit",
                        created_by_tg_user_id=safe_user_id,
                        meta={
                            "publication_id": safe_publication_id,
                            "edited_from_revision": safe_expected_revision,
                        },
                    )
                )
                item.current_revision = next_revision
                publication.content_revision = next_revision
                schedule.content_revision = next_revision

                # Compatibility-only projection. Never mutate status, schedule time,
                # dedupe identity or execution metadata here.
                task.payload = projected_payload

                await session.commit()
                return QueuedCanonicalPublicationEditResult(
                    publication_id=safe_publication_id,
                    content_item_id=int(item.id),
                    previous_revision=safe_expected_revision,
                    revision=next_revision,
                )
            except Exception:
                await session.rollback()
                raise
