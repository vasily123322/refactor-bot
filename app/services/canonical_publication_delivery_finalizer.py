from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
)
from app.services.scheduler_errors import SAFE_DELIVERY_ERROR, public_scheduler_error
from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryFinalizeResult:
    publication_id: int
    outcome: Literal["published", "failed", "conflict", "invalid"]
    attempt: int | None = None


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


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


class CanonicalPublicationDeliveryFinalizer:
    """Finalize one claimed canonical Publication under its exact live delivery lease.

    Success is stricter than failure because a Telegram side effect has already happened:
    the immutable plan returned by claim must still match the locked canonical delivery
    intent before terminal ``published`` state can be committed. Any drift leaves the
    claim ambiguous for expiry recovery without automatic resend.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _locked_state(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        at: datetime,
    ) -> tuple[
        PublicationDeliveryLease,
        Publication,
        ScheduleEntry,
        ContentItem,
        ContentRevision,
        Channel,
        PublicationAttempt,
    ] | None:
        try:
            publication_id = int(handle.publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if publication_id <= 0 or not str(handle.lease_token):
            return None

        current = _utc(at)
        lease = (
            await self.session.execute(
                select(PublicationDeliveryLease)
                .where(
                    PublicationDeliveryLease.publication_id == publication_id,
                    PublicationDeliveryLease.lease_token == str(handle.lease_token),
                    PublicationDeliveryLease.expires_at > current,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            return None

        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.id == publication_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            publication is None
            or publication.status != "sending"
            or int(publication.attempt_count or 0) != 1
            or publication.schedule_entry_id is None
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
        ):
            return None

        schedule = (
            await self.session.execute(
                select(ScheduleEntry)
                .where(
                    ScheduleEntry.id == int(publication.schedule_entry_id),
                    ScheduleEntry.channel_id == int(publication.channel_id),
                    ScheduleEntry.content_item_id == int(publication.content_item_id),
                    ScheduleEntry.content_revision == int(publication.content_revision),
                    ScheduleEntry.status == "pending",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if schedule is None:
            return None

        item = (
            await self.session.execute(
                select(ContentItem)
                .where(ContentItem.id == int(publication.content_item_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            return None

        revision = (
            await self.session.execute(
                select(ContentRevision)
                .where(
                    ContentRevision.content_item_id == int(publication.content_item_id),
                    ContentRevision.revision == int(publication.content_revision),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if revision is None:
            return None

        channel = (
            await self.session.execute(
                select(Channel)
                .where(Channel.id == int(publication.channel_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if channel is None:
            return None

        attempt = (
            await self.session.execute(
                select(PublicationAttempt)
                .where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            attempt is None
            or attempt.status != "sending"
            or attempt.finished_at is not None
            or attempt.telegram_message_ids is not None
            or attempt.error is not None
            or dict(attempt.meta or {}).get("canonical_delivery") is not True
        ):
            return None

        return lease, publication, schedule, item, revision, channel, attempt

    @staticmethod
    def _intent_matches(
        plan: CanonicalPublicationDeliveryPlan,
        *,
        publication: Publication,
        schedule: ScheduleEntry,
        item: ContentItem,
        revision: ContentRevision,
        channel: Channel,
    ) -> bool:
        try:
            if (
                int(plan.publication_id) != int(publication.id)
                or int(plan.schedule_entry_id) != int(schedule.id)
                or int(plan.schedule_entry_id) != int(publication.schedule_entry_id or 0)
                or int(plan.channel_id) != int(publication.channel_id)
                or int(plan.channel_id) != int(schedule.channel_id)
                or int(plan.channel_id) != int(item.channel_id)
                or int(plan.channel_id) != int(channel.id)
                or int(plan.telegram_chat_id) != int(channel.tg_chat_id)
                or channel.is_active is not True
                or str(item.kind) != "post"
                or int(plan.content_item_id) != int(publication.content_item_id)
                or int(plan.content_item_id) != int(schedule.content_item_id)
                or int(plan.content_item_id) != int(item.id)
                or int(plan.content_revision) != int(publication.content_revision)
                or int(plan.content_revision) != int(schedule.content_revision)
                or int(plan.content_revision) != int(revision.revision)
                or int(plan.content_item_id) != int(revision.content_item_id)
                or _utc(plan.scheduled_at) != _utc(schedule.scheduled_at)
                or plan.timezone != schedule.timezone
            ):
                return False
        except (TypeError, ValueError, OverflowError):
            return False

        publication_meta = _mapping(publication.meta)
        schedule_meta = _mapping(schedule.meta)
        repeat_rule = _mapping(schedule.repeat_rule)
        if publication_meta is None or schedule_meta is None or repeat_rule is None:
            return False
        runtime_options = _runtime_options(publication_meta, schedule_meta)
        if runtime_options is None:
            return False

        try:
            document = PostDocument.from_dict(revision.document)
        except (TypeError, ValueError):
            return False

        return (
            _json_snapshot(runtime_options) == plan.runtime_options_snapshot
            and _json_snapshot(repeat_rule) == plan.repeat_rule_snapshot
            and _json_snapshot(document.to_dict()) == plan.document_snapshot
        )

    async def complete_success(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        plan: CanonicalPublicationDeliveryPlan,
        message_ids: Any,
        result_link: Any = None,
        finished_at: datetime | None = None,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryFinalizeResult:
        try:
            publication_id = int(handle.publication_id)
        except (TypeError, ValueError, OverflowError):
            publication_id = 0

        ids = normalize_telegram_message_ids(message_ids)
        if not ids:
            return CanonicalPublicationDeliveryFinalizeResult(
                publication_id=publication_id,
                outcome="invalid",
            )

        normalized_link: str | None = None
        if result_link is not None:
            normalized_link = normalize_telegram_result_link(result_link)
            if normalized_link is None:
                return CanonicalPublicationDeliveryFinalizeResult(
                    publication_id=publication_id,
                    outcome="invalid",
                )

        if not isinstance(plan, CanonicalPublicationDeliveryPlan):
            return CanonicalPublicationDeliveryFinalizeResult(
                publication_id=publication_id,
                outcome="invalid",
            )

        current = _utc(now)
        try:
            state = await self._locked_state(handle, at=current)
            if state is None:
                await self.session.rollback()
                return CanonicalPublicationDeliveryFinalizeResult(
                    publication_id=publication_id,
                    outcome="conflict",
                )
            lease, publication, schedule, item, revision, channel, attempt = state
            if not self._intent_matches(
                plan,
                publication=publication,
                schedule=schedule,
                item=item,
                revision=revision,
                channel=channel,
            ):
                await self.session.rollback()
                return CanonicalPublicationDeliveryFinalizeResult(
                    publication_id=publication_id,
                    outcome="conflict",
                )

            completed_at = _utc(finished_at or current)
            publication.status = "published"
            publication.telegram_message_ids = list(ids)
            publication.result_link = normalized_link
            publication.last_error = None
            schedule.status = "completed"
            attempt.status = "published"
            attempt.telegram_message_ids = list(ids)
            attempt.error = None
            attempt.finished_at = completed_at
            await self.session.delete(lease)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return CanonicalPublicationDeliveryFinalizeResult(
            publication_id=publication_id,
            outcome="published",
            attempt=1,
        )

    async def complete_failure(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        error: Any = SAFE_DELIVERY_ERROR,
        finished_at: datetime | None = None,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryFinalizeResult:
        try:
            publication_id = int(handle.publication_id)
        except (TypeError, ValueError, OverflowError):
            publication_id = 0
        safe_error = public_scheduler_error(error)
        current = _utc(now)

        try:
            state = await self._locked_state(handle, at=current)
            if state is None:
                await self.session.rollback()
                return CanonicalPublicationDeliveryFinalizeResult(
                    publication_id=publication_id,
                    outcome="conflict",
                )
            lease, publication, schedule, _item, _revision, _channel, attempt = state

            completed_at = _utc(finished_at or current)
            publication.status = "failed"
            publication.telegram_message_ids = None
            publication.result_link = None
            publication.last_error = safe_error
            schedule.status = "failed"
            attempt.status = "failed"
            attempt.telegram_message_ids = None
            attempt.error = safe_error
            attempt.finished_at = completed_at
            await self.session.delete(lease)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return CanonicalPublicationDeliveryFinalizeResult(
            publication_id=publication_id,
            outcome="failed",
            attempt=1,
        )
