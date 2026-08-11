from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.publication_bridge import (
    PublicationBridgeError,
    _delivery_meta,
    _runtime_intent,
    _scheduler_payload,
)
from app.services.rich_media_assets import RichMediaAssetError, RichMediaAssetResolver
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatTransportResult:
    source_publication_id: int
    outcome: Literal[
        "created",
        "existing",
        "existing_transport",
        "ineligible",
        "conflict",
    ]
    publication_id: int | None = None
    schedule_entry_id: int | None = None
    legacy_post_task_id: int | None = None


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _scheduled_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 128:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return as_utc(datetime.fromisoformat(text))
    except (TypeError, ValueError, OverflowError):
        return None


def _dedupe_key(source_publication_id: int, scheduled_at: datetime) -> str:
    return f"canonical-repeat:{int(source_publication_id)}:{as_utc(scheduled_at).isoformat()}"


class CanonicalRepeatTransportAdapter:
    """Materialize one reserved canonical repeat plan into legacy transport.

    Canonical reservation is the planning authority. The resulting PostTask remains the
    delivery adapter/executor for this migration stage. The service is intentionally
    not wired into runtime yet; legacy repeat scheduling remains authoritative until a
    later guarded cutover.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_source(
        self,
        source_publication_id: int,
    ) -> tuple[Publication, ScheduleEntry] | None:
        return (
            await self.session.execute(
                select(Publication, ScheduleEntry)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .where(Publication.id == int(source_publication_id))
                .with_for_update()
            )
        ).one_or_none()

    async def _exact_transport_ids(
        self,
        *,
        channel_id: int,
        repeat_group_id: int,
        scheduled_at: datetime,
    ) -> tuple[int, ...]:
        rows = (
            await self.session.execute(
                select(PostTask)
                .where(
                    PostTask.channel_id == int(channel_id),
                    PostTask.payload["repeat_group_id"].as_integer()
                    == int(repeat_group_id),
                )
                .order_by(PostTask.id.asc())
                .limit(200)
            )
        ).scalars().all()
        expected = as_utc(scheduled_at)
        return tuple(
            int(task.id)
            for task in rows
            if task.scheduled_at is not None and as_utc(task.scheduled_at) == expected
        )

    async def _existing_after_integrity_conflict(
        self,
        source_publication_id: int,
    ) -> CanonicalRepeatTransportResult:
        verification = await CanonicalRepeatReservationVerifier(self.session).verify(
            int(source_publication_id)
        )
        if verification.outcome == "matched":
            return CanonicalRepeatTransportResult(
                source_publication_id=int(source_publication_id),
                outcome="existing",
                publication_id=verification.successor_publication_id,
                schedule_entry_id=verification.successor_schedule_entry_id,
                legacy_post_task_id=verification.successor_legacy_post_task_id,
            )
        return CanonicalRepeatTransportResult(
            source_publication_id=int(source_publication_id),
            outcome="conflict",
        )

    async def materialize(
        self,
        source_publication_id: int,
    ) -> CanonicalRepeatTransportResult:
        try:
            safe_source_id = int(source_publication_id)
        except (TypeError, ValueError, OverflowError):
            return CanonicalRepeatTransportResult(0, "ineligible")
        if safe_source_id <= 0:
            return CanonicalRepeatTransportResult(safe_source_id, "ineligible")

        locked = await self._lock_source(safe_source_id)
        if locked is None:
            await self.session.rollback()
            return CanonicalRepeatTransportResult(safe_source_id, "ineligible")
        source, source_schedule = locked

        verification = await CanonicalRepeatReservationVerifier(self.session).verify(
            safe_source_id
        )
        if verification.outcome == "matched":
            await self.session.rollback()
            return CanonicalRepeatTransportResult(
                source_publication_id=safe_source_id,
                outcome="existing",
                publication_id=verification.successor_publication_id,
                schedule_entry_id=verification.successor_schedule_entry_id,
                legacy_post_task_id=verification.successor_legacy_post_task_id,
            )
        if verification.outcome != "pending":
            await self.session.rollback()
            return CanonicalRepeatTransportResult(
                source_publication_id=safe_source_id,
                outcome=(
                    "ineligible" if verification.outcome == "ineligible" else "conflict"
                ),
            )

        source_meta = _mapping(source.meta)
        source_schedule_meta = _mapping(source_schedule.meta)
        if source_meta is None or source_schedule_meta is None:
            await self.session.rollback()
            return CanonicalRepeatTransportResult(safe_source_id, "conflict")
        reservation = _mapping(
            source_meta.get(CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY)
        )
        if reservation is None or reservation != _mapping(
            source_schedule_meta.get(CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY)
        ):
            await self.session.rollback()
            return CanonicalRepeatTransportResult(safe_source_id, "conflict")

        repeat_group_id = _positive_int(reservation.get("repeat_group_id"))
        channel_id = _positive_int(reservation.get("channel_id"))
        content_item_id = _positive_int(reservation.get("content_item_id"))
        content_revision = _positive_int(reservation.get("content_revision"))
        repeat_seconds = _positive_int(reservation.get("repeat_seconds"))
        expected_at = _scheduled_at(reservation.get("scheduled_at"))
        runtime_options = _mapping(reservation.get("runtime_options"))
        if None in (
            repeat_group_id,
            channel_id,
            content_item_id,
            content_revision,
            repeat_seconds,
            expected_at,
            runtime_options,
        ):
            await self.session.rollback()
            return CanonicalRepeatTransportResult(safe_source_id, "conflict")
        assert repeat_group_id is not None
        assert channel_id is not None
        assert content_item_id is not None
        assert content_revision is not None
        assert repeat_seconds is not None
        assert expected_at is not None
        assert runtime_options is not None

        transport_ids = await self._exact_transport_ids(
            channel_id=channel_id,
            repeat_group_id=repeat_group_id,
            scheduled_at=expected_at,
        )
        if transport_ids:
            await self.session.rollback()
            return CanonicalRepeatTransportResult(
                source_publication_id=safe_source_id,
                outcome=("existing_transport" if len(transport_ids) == 1 else "conflict"),
                legacy_post_task_id=(transport_ids[0] if len(transport_ids) == 1 else None),
            )

        item = await self.session.get(ContentItem, content_item_id)
        revision = (
            await self.session.execute(
                select(ContentRevision).where(
                    ContentRevision.content_item_id == content_item_id,
                    ContentRevision.revision == content_revision,
                )
            )
        ).scalar_one_or_none()
        if (
            item is None
            or revision is None
            or int(item.channel_id) != channel_id
            or int(source.channel_id) != channel_id
            or int(source.content_item_id) != content_item_id
            or int(source.content_revision) != content_revision
        ):
            await self.session.rollback()
            return CanonicalRepeatTransportResult(safe_source_id, "conflict")

        try:
            document = PostDocument.from_dict(revision.document)
            render_document = await RichMediaAssetResolver(self.session).resolve(
                document,
                channel_id=channel_id,
            )
            payload = _scheduler_payload(document, render_document=render_document)
            payload["repeat_on"] = True
            payload["repeat_seconds"] = repeat_seconds
            payload["repeat_group_id"] = repeat_group_id
            runtime_intent = _runtime_intent(runtime_options, payload=payload)
            for key, value in runtime_intent.items():
                payload[key] = deepcopy(value)
        except (PublicationBridgeError, RichMediaAssetError, ValueError, TypeError):
            await self.session.rollback()
            return CanonicalRepeatTransportResult(safe_source_id, "conflict")

        autodelete_seconds = _positive_int(runtime_intent.get("autodelete_seconds"))
        if autodelete_seconds is not None:
            payload["autodelete_at"] = (
                expected_at + timedelta(seconds=autodelete_seconds)
            ).isoformat()

        canonical_meta = _delivery_meta(None, runtime_intent)
        canonical_meta = {
            **canonical_meta,
            "repeat_group_id": repeat_group_id,
            "canonical_repeat_source_publication_id": safe_source_id,
            "canonical_repeat_transport_adapter": True,
            "reused_content_provenance": True,
            "repeat_root_provenance": True,
        }
        schedule_meta = deepcopy(canonical_meta)
        schedule = ScheduleEntry(
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            scheduled_at=expected_at,
            timezone=source_schedule.timezone,
            status="pending",
            repeat_rule={"enabled": True, "seconds": repeat_seconds},
            meta=schedule_meta,
        )
        publication = Publication(
            schedule_entry_id=None,
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            status="queued",
            meta=deepcopy(canonical_meta),
        )
        self.session.add_all([schedule, publication])

        try:
            await self.session.flush()
            publication.schedule_entry_id = int(schedule.id)
            task = PostTask(
                channel_id=channel_id,
                status="pending",
                payload=payload,
                dedupe_key=_dedupe_key(safe_source_id, expected_at),
                scheduled_at=expected_at,
            )
            self.session.add(task)
            await self.session.flush()
            task_id = int(task.id)
            publication.legacy_post_task_id = task_id
            schedule.meta = {
                **dict(schedule.meta or {}),
                "legacy_post_task_id": task_id,
            }
            await self.session.commit()
            return CanonicalRepeatTransportResult(
                source_publication_id=safe_source_id,
                outcome="created",
                publication_id=int(publication.id),
                schedule_entry_id=int(schedule.id),
                legacy_post_task_id=task_id,
            )
        except IntegrityError:
            await self.session.rollback()
            return await self._existing_after_integrity_conflict(safe_source_id)
        except Exception:
            await self.session.rollback()
            raise
