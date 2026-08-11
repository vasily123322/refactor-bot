from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryExpiredLeaseRef,
)
from app.services.scheduler_errors import UNKNOWN_DELIVERY_ERROR
from app.services.scheduling import as_utc


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryRecoveryPlan:
    publication_id: int
    lease_token: str
    attempt: int
    error: str


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryRecoveryResult:
    publication_id: int
    outcome: Literal["ambiguous", "not_expired", "conflict"]
    plan: CanonicalPublicationDeliveryRecoveryPlan | None = None


class CanonicalPublicationDeliveryRecoveryPlanner:
    """Read-only proof for fail-closed recovery of an expired delivery lease.

    Expiry after a canonical Publication entered ``sending`` means Telegram may have
    accepted a side effect that was not durably finalized. The planner therefore never
    returns a retry command. A proven expired claim yields only an ``ambiguous`` plan
    carrying the fixed public-safe failure reason used by recovery.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        reference: CanonicalPublicationDeliveryExpiredLeaseRef,
        *,
        at: datetime | None = None,
    ) -> CanonicalPublicationDeliveryRecoveryResult:
        try:
            publication_id = int(reference.publication_id)
        except (TypeError, ValueError, OverflowError):
            publication_id = 0
        if publication_id <= 0 or not str(reference.lease_token):
            return CanonicalPublicationDeliveryRecoveryResult(
                publication_id=publication_id,
                outcome="conflict",
            )

        current = as_utc(at or datetime.now(timezone.utc))
        lease = (
            await self.session.execute(
                select(PublicationDeliveryLease).where(
                    PublicationDeliveryLease.publication_id == publication_id,
                    PublicationDeliveryLease.lease_token == str(reference.lease_token),
                )
            )
        ).scalar_one_or_none()
        if lease is None:
            return CanonicalPublicationDeliveryRecoveryResult(
                publication_id=publication_id,
                outcome="conflict",
            )
        if as_utc(lease.expires_at) > current:
            return CanonicalPublicationDeliveryRecoveryResult(
                publication_id=publication_id,
                outcome="not_expired",
            )

        publication = await self.session.get(Publication, publication_id)
        if (
            publication is None
            or publication.status != "sending"
            or int(publication.attempt_count or 0) != 1
            or publication.schedule_entry_id is None
            or publication.telegram_message_ids not in (None, [])
            or publication.result_link is not None
            or publication.last_error is not None
        ):
            return CanonicalPublicationDeliveryRecoveryResult(
                publication_id=publication_id,
                outcome="conflict",
            )

        schedule = await self.session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id),
        )
        if (
            schedule is None
            or schedule.status != "pending"
            or int(schedule.channel_id) != int(publication.channel_id)
            or int(schedule.content_item_id) != int(publication.content_item_id)
            or int(schedule.content_revision) != int(publication.content_revision)
        ):
            return CanonicalPublicationDeliveryRecoveryResult(
                publication_id=publication_id,
                outcome="conflict",
            )

        attempt = (
            await self.session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                )
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
            return CanonicalPublicationDeliveryRecoveryResult(
                publication_id=publication_id,
                outcome="conflict",
            )

        recovery = CanonicalPublicationDeliveryRecoveryPlan(
            publication_id=publication_id,
            lease_token=str(lease.lease_token),
            attempt=1,
            error=UNKNOWN_DELIVERY_ERROR,
        )
        return CanonicalPublicationDeliveryRecoveryResult(
            publication_id=publication_id,
            outcome="ambiguous",
            plan=recovery,
        )
