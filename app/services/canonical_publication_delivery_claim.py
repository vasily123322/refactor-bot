from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.models import Channel
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
    CanonicalPublicationDeliveryPlanner,
)


DEFAULT_PUBLICATION_DELIVERY_LEASE_SECONDS = 180
DEFAULT_PUBLICATION_DELIVERY_RECOVERY_LEASE_SECONDS = 60


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _ttl(value: int, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = DEFAULT_PUBLICATION_DELIVERY_LEASE_SECONDS
    return max(30, min(parsed, maximum))


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryLeaseHandle:
    publication_id: int
    lease_token: str
    holder: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryExpiredLeaseRef:
    publication_id: int
    lease_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryClaimRequirements:
    """Optional execution/cutover restrictions applied inside the claim transaction."""

    require_empty_runtime_options: bool = False
    require_nonrepeat: bool = False
    require_transport_retired: bool = False


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryClaim:
    plan: CanonicalPublicationDeliveryPlan
    lease: CanonicalPublicationDeliveryLeaseHandle
    attempt: int


class CanonicalPublicationDeliveryClaimService:
    """Atomic canonical Publication execution claim and durable liveness lease.

    The pure delivery planner remains the eligibility proof. Claim acquires locks for
    every mutable row used by that proof, re-runs the planner under those locks, then
    optionally applies execution/cutover restrictions before atomically transitioning
    Publication ``queued -> sending``, creating unfinished PublicationAttempt #1 and
    inserting the typed delivery lease. Any conflict rolls the whole transaction back.

    Runtime authority restrictions live here rather than in the canonical planner so
    canonical eligibility remains transport-independent. In particular a caller may
    require ``legacy_post_task_id IS NULL`` under the Publication row lock before it is
    allowed to take delivery authority from the legacy scheduler.

    Normal claim never deletes/reuses an expired lease. An expired delivery lease may
    represent an ambiguous Telegram side effect and is therefore a recovery barrier.
    ``take_expired`` only transfers recovery ownership; it never retries delivery.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_delivery_rows(
        self,
        publication_id: int,
    ) -> tuple[Publication, ScheduleEntry] | None:
        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.id == int(publication_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None or publication.schedule_entry_id is None:
            return None

        schedule = (
            await self.session.execute(
                select(ScheduleEntry)
                .where(ScheduleEntry.id == int(publication.schedule_entry_id))
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
        return publication, schedule

    @staticmethod
    def _meets_requirements(
        plan: CanonicalPublicationDeliveryPlan,
        publication: Publication,
        schedule: ScheduleEntry,
        requirements: CanonicalPublicationDeliveryClaimRequirements | None,
    ) -> bool:
        if requirements is None:
            return True
        if (
            requirements.require_transport_retired
            and publication.legacy_post_task_id is not None
        ):
            return False
        if requirements.require_empty_runtime_options:
            try:
                if plan.runtime_options():
                    return False
            except (TypeError, ValueError):
                return False
        if requirements.require_nonrepeat:
            raw_rule = schedule.repeat_rule
            if raw_rule is not None and not isinstance(raw_rule, Mapping):
                return False
            rule = dict(raw_rule or {})
            enabled = rule.get("enabled")
            if enabled is not None and enabled is not False:
                return False
        return True

    async def claim(
        self,
        *,
        publication_id: int,
        holder: str,
        ttl_seconds: int = DEFAULT_PUBLICATION_DELIVERY_LEASE_SECONDS,
        now: datetime | None = None,
        requirements: CanonicalPublicationDeliveryClaimRequirements | None = None,
    ) -> CanonicalPublicationDeliveryClaim | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        current = _utc(now)
        expires_at = current + timedelta(seconds=_ttl(ttl_seconds, maximum=3600))
        token = uuid.uuid4().hex
        holder_value = str(holder).strip()[:64] or "publication-delivery"

        try:
            locked = await self._lock_delivery_rows(safe_publication_id)
            if locked is None:
                await self.session.rollback()
                return None
            publication, schedule = locked

            plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
                safe_publication_id,
                at=current,
            )
            if plan is None:
                await self.session.rollback()
                return None
            if (
                int(plan.schedule_entry_id) != int(publication.schedule_entry_id or 0)
                or int(plan.content_item_id) != int(publication.content_item_id)
                or int(plan.content_revision) != int(publication.content_revision)
                or int(plan.channel_id) != int(publication.channel_id)
                or not self._meets_requirements(
                    plan,
                    publication,
                    schedule,
                    requirements,
                )
            ):
                await self.session.rollback()
                return None

            claimed = await self.session.execute(
                update(Publication)
                .where(
                    Publication.id == safe_publication_id,
                    Publication.status == "queued",
                    Publication.attempt_count == 0,
                )
                .values(status="sending", attempt_count=1)
                .execution_options(synchronize_session="fetch")
            )
            if int(claimed.rowcount or 0) != 1:
                await self.session.rollback()
                return None

            self.session.add_all(
                [
                    PublicationAttempt(
                        publication_id=safe_publication_id,
                        attempt=1,
                        status="sending",
                        telegram_message_ids=None,
                        error=None,
                        meta={"canonical_delivery": True},
                        finished_at=None,
                    ),
                    PublicationDeliveryLease(
                        publication_id=safe_publication_id,
                        lease_token=token,
                        holder=holder_value,
                        expires_at=expires_at,
                    ),
                ]
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None
        except Exception:
            await self.session.rollback()
            raise

        return CanonicalPublicationDeliveryClaim(
            plan=plan,
            lease=CanonicalPublicationDeliveryLeaseHandle(
                publication_id=safe_publication_id,
                lease_token=token,
                holder=holder_value,
                expires_at=expires_at,
            ),
            attempt=1,
        )

    async def expired(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[CanonicalPublicationDeliveryExpiredLeaseRef]:
        current = _utc(now)
        try:
            bounded_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 100
        rows = (
            await self.session.execute(
                select(
                    PublicationDeliveryLease.publication_id,
                    PublicationDeliveryLease.lease_token,
                    PublicationDeliveryLease.expires_at,
                )
                .where(PublicationDeliveryLease.expires_at <= current)
                .order_by(
                    PublicationDeliveryLease.expires_at.asc(),
                    PublicationDeliveryLease.publication_id.asc(),
                )
                .limit(bounded_limit)
            )
        ).all()
        return [
            CanonicalPublicationDeliveryExpiredLeaseRef(
                publication_id=int(row.publication_id),
                lease_token=str(row.lease_token),
                expires_at=_utc(row.expires_at),
            )
            for row in rows
        ]

    async def take_expired(
        self,
        reference: CanonicalPublicationDeliveryExpiredLeaseRef,
        *,
        holder: str = "publication-delivery-recovery",
        ttl_seconds: int = DEFAULT_PUBLICATION_DELIVERY_RECOVERY_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryLeaseHandle | None:
        current = _utc(now)
        expires_at = current + timedelta(seconds=_ttl(ttl_seconds, maximum=600))
        token = uuid.uuid4().hex
        holder_value = str(holder).strip()[:64] or "publication-delivery-recovery"
        try:
            result = await self.session.execute(
                update(PublicationDeliveryLease)
                .where(
                    PublicationDeliveryLease.publication_id
                    == int(reference.publication_id),
                    PublicationDeliveryLease.lease_token == str(reference.lease_token),
                    PublicationDeliveryLease.expires_at <= current,
                )
                .values(
                    lease_token=token,
                    holder=holder_value,
                    expires_at=expires_at,
                )
                .execution_options(synchronize_session=False)
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None
        except Exception:
            await self.session.rollback()
            raise
        if int(result.rowcount or 0) != 1:
            return None
        return CanonicalPublicationDeliveryLeaseHandle(
            publication_id=int(reference.publication_id),
            lease_token=token,
            holder=holder_value,
            expires_at=expires_at,
        )

    async def renew(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        ttl_seconds: int = DEFAULT_PUBLICATION_DELIVERY_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryLeaseHandle | None:
        current = _utc(now)
        expires_at = current + timedelta(seconds=_ttl(ttl_seconds, maximum=3600))
        result = await self.session.execute(
            update(PublicationDeliveryLease)
            .where(
                PublicationDeliveryLease.publication_id == int(handle.publication_id),
                PublicationDeliveryLease.lease_token == str(handle.lease_token),
                PublicationDeliveryLease.expires_at > current,
            )
            .values(expires_at=expires_at)
            .execution_options(synchronize_session=False)
        )
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        if int(result.rowcount or 0) != 1:
            return None
        return CanonicalPublicationDeliveryLeaseHandle(
            publication_id=int(handle.publication_id),
            lease_token=str(handle.lease_token),
            holder=str(handle.holder),
            expires_at=expires_at,
        )

    async def release(self, handle: CanonicalPublicationDeliveryLeaseHandle) -> bool:
        result = await self.session.execute(
            delete(PublicationDeliveryLease).where(
                PublicationDeliveryLease.publication_id == int(handle.publication_id),
                PublicationDeliveryLease.lease_token == str(handle.lease_token),
            )
        )
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return bool(int(result.rowcount or 0))

    async def current(self, publication_id: int) -> PublicationDeliveryLease | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        return (
            await self.session.execute(
                select(PublicationDeliveryLease).where(
                    PublicationDeliveryLease.publication_id == safe_publication_id
                )
            )
        ).scalar_one_or_none()
