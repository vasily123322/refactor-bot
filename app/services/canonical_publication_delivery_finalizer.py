from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
)
from app.services.canonical_publication_delivery_runtime import (
    CanonicalPublicationDeliveryRuntimeConflict,
    apply_canonical_delivery_success_runtime,
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


class CanonicalPublicationDeliveryFinalizer:
    """Finalize one claimed canonical Publication under its exact live delivery lease.

    This service owns only durable canonical lifecycle state. It performs no Telegram
    calls and does not read or write ``PostTask``. Exact lease-token matching prevents
    a stale worker from committing after recovery has taken ownership. An expired lease
    is also a hard barrier: ambiguous delivery is resolved only after recovery takes a
    fresh token through ``CanonicalPublicationDeliveryClaimService.take_expired()``.
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

        return lease, publication, schedule, attempt

    async def complete_success(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        message_ids: Any,
        result_link: Any = None,
        plan: CanonicalPublicationDeliveryPlan | None = None,
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

        current = _utc(now)
        try:
            state = await self._locked_state(handle, at=current)
            if state is None:
                await self.session.rollback()
                return CanonicalPublicationDeliveryFinalizeResult(
                    publication_id=publication_id,
                    outcome="conflict",
                )
            lease, publication, schedule, attempt = state

            completed_at = _utc(finished_at or current)
            if plan is not None:
                try:
                    apply_canonical_delivery_success_runtime(
                        publication,
                        schedule,
                        plan,
                        delivered_at=completed_at,
                    )
                except CanonicalPublicationDeliveryRuntimeConflict:
                    await self.session.rollback()
                    return CanonicalPublicationDeliveryFinalizeResult(
                        publication_id=publication_id,
                        outcome="conflict",
                    )

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
            lease, publication, schedule, attempt = state

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
