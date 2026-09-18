from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.publication_bridge import _delivery_meta
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    canonical_repeat_runtime_options_supported,
)
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalRepeatTransportResult:
    source_publication_id: int
    outcome: Literal[
        "created",
        "existing",
        "ineligible",
        "conflict",
    ]
    publication_id: int | None = None
    schedule_entry_id: int | None = None


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


class CanonicalRepeatTransportAdapter:
    """Materialize one reserved canonical repeat plan into canonical durable state.

    Reservation is the planning authority. Exact content, schedule, repeat cadence,
    runtime intent, ownership mode and source lineage are persisted on canonical rows.
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

        # This Stage 5 cutover is canonical-only. Intentional legacy fallback (including
        # time+views/report) keeps its existing legacy scheduler path and is never
        # silently converted into canonical ownership here.
        if not canonical_repeat_runtime_options_supported(runtime_options):
            await self.session.rollback()
            return CanonicalRepeatTransportResult(safe_source_id, "conflict")

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

        canonical_meta = _delivery_meta(None, runtime_options)
        canonical_meta = {
            **canonical_meta,
            "repeat_group_id": repeat_group_id,
            "canonical_repeat_source_publication_id": safe_source_id,
            "canonical_repeat_transport_adapter": True,
            "canonical_repeat_posttask_free": True,
            "reused_content_provenance": True,
            "repeat_root_provenance": True,
        }
        schedule = ScheduleEntry(
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            scheduled_at=expected_at,
            timezone=source_schedule.timezone,
            status="pending",
            repeat_rule={"enabled": True, "seconds": repeat_seconds},
            meta=deepcopy(canonical_meta),
        )
        publication = Publication(
            schedule_entry_id=None,
            content_item_id=content_item_id,
            content_revision=content_revision,
            channel_id=channel_id,
            status="queued",
            execution_mode=CANONICAL_EXECUTION_MODE,
            repeat_source_publication_id=safe_source_id,
            meta=deepcopy(canonical_meta),
        )
        self.session.add_all([schedule, publication])

        try:
            await self.session.flush()
            publication.schedule_entry_id = int(schedule.id)
            await self.session.commit()
            return CanonicalRepeatTransportResult(
                source_publication_id=safe_source_id,
                outcome="created",
                publication_id=int(publication.id),
                schedule_entry_id=int(schedule.id),
            )
        except IntegrityError:
            await self.session.rollback()
            return await self._existing_after_integrity_conflict(safe_source_id)
        except Exception:
            await self.session.rollback()
            raise
