from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry


HEALTHY_LIVE_LEASE = "healthy_live_lease"
LEGACY_TRANSPORT_RELINKED = "legacy_transport_relinked"
LEGACY_TRANSPORT_LINKED = "legacy_transport_linked"
DURABLE_DELIVERY_EVIDENCE = "durable_delivery_evidence"
SCHEDULE_MISMATCH = "schedule_mismatch"
ATTEMPT_MISMATCH = "attempt_mismatch"
MISSING_LEASE = "missing_lease"
EXPIRED_LEASE = "expired_lease"

_FINDING_PRECEDENCE = (
    LEGACY_TRANSPORT_RELINKED,
    LEGACY_TRANSPORT_LINKED,
    DURABLE_DELIVERY_EVIDENCE,
    SCHEDULE_MISMATCH,
    ATTEMPT_MISMATCH,
    MISSING_LEASE,
    EXPIRED_LEASE,
)


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _bounded_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = 100
    return max(1, min(parsed, 500))


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryInflightAuditItem:
    publication_id: int
    classification: str
    findings: tuple[str, ...]
    lease_state: str
    lease_expires_at: datetime | None
    legacy_post_task_id: int | None
    schedule_entry_id: int | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryInflightAuditPage:
    items: tuple[CanonicalPublicationDeliveryInflightAuditItem, ...]
    next_publication_id: int | None
    done: bool


class CanonicalPublicationDeliveryInflightAuditService:
    """Read-only bounded classification of canonical `sending` lifecycle state.

    This service deliberately has no repair, takeover, release, finalization or provider
    API. It also never exposes lease tokens. Its only purpose is to identify historical
    or partial in-flight states before any orphan-adoption policy is considered.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _schedule_mismatch(
        publication: Publication,
        schedule: ScheduleEntry | None,
    ) -> bool:
        if schedule is None:
            return True
        return bool(
            schedule.status != "pending"
            or int(schedule.content_item_id) != int(publication.content_item_id)
            or int(schedule.content_revision) != int(publication.content_revision)
            or int(schedule.channel_id) != int(publication.channel_id)
        )

    @staticmethod
    def _canonical_attempt_present(attempts: list[PublicationAttempt]) -> bool:
        for attempt in attempts:
            meta = attempt.meta
            if isinstance(meta, Mapping) and meta.get("canonical_delivery") is True:
                return True
        return False

    @staticmethod
    def _attempt_mismatch(
        publication: Publication,
        attempts: list[PublicationAttempt],
    ) -> bool:
        if int(publication.attempt_count or 0) != 1 or len(attempts) != 1:
            return True
        attempt = attempts[0]
        meta = attempt.meta
        canonical_marker = (
            meta.get("canonical_delivery")
            if isinstance(meta, Mapping)
            else None
        )
        return bool(
            int(attempt.attempt) != 1
            or attempt.status != "sending"
            or canonical_marker is not True
        )

    @staticmethod
    def _has_durable_delivery_evidence(
        publication: Publication,
        attempts: list[PublicationAttempt],
    ) -> bool:
        if (
            publication.telegram_message_ids is not None
            or publication.result_link is not None
            or publication.last_error is not None
        ):
            return True
        return any(
            attempt.telegram_message_ids is not None
            or attempt.error is not None
            or attempt.finished_at is not None
            for attempt in attempts
        )

    @staticmethod
    def _ordered_findings(findings: set[str]) -> tuple[str, ...]:
        return tuple(name for name in _FINDING_PRECEDENCE if name in findings)

    async def scan_page(
        self,
        *,
        limit: int = 100,
        after_publication_id: int | None = None,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryInflightAuditPage:
        current = _utc(now)
        bounded = _bounded_limit(limit)

        statement = select(Publication).where(Publication.status == "sending")
        if after_publication_id is not None:
            try:
                cursor = int(after_publication_id)
            except (TypeError, ValueError, OverflowError):
                cursor = 0
            if cursor > 0:
                statement = statement.where(Publication.id > cursor)
        publications = list(
            (
                await self.session.execute(
                    statement.order_by(Publication.id.asc()).limit(bounded + 1)
                )
            ).scalars()
        )
        done = len(publications) <= bounded
        publications = publications[:bounded]
        if not publications:
            return CanonicalPublicationDeliveryInflightAuditPage(
                items=(),
                next_publication_id=None,
                done=True,
            )

        publication_ids = [int(publication.id) for publication in publications]
        schedule_ids = [
            int(publication.schedule_entry_id)
            for publication in publications
            if publication.schedule_entry_id is not None
        ]

        leases = {
            int(lease.publication_id): lease
            for lease in (
                await self.session.execute(
                    select(PublicationDeliveryLease).where(
                        PublicationDeliveryLease.publication_id.in_(publication_ids)
                    )
                )
            ).scalars()
        }
        schedules = (
            {
                int(schedule.id): schedule
                for schedule in (
                    await self.session.execute(
                        select(ScheduleEntry).where(ScheduleEntry.id.in_(schedule_ids))
                    )
                ).scalars()
            }
            if schedule_ids
            else {}
        )
        attempts_by_publication: dict[int, list[PublicationAttempt]] = defaultdict(list)
        for attempt in (
            await self.session.execute(
                select(PublicationAttempt)
                .where(PublicationAttempt.publication_id.in_(publication_ids))
                .order_by(
                    PublicationAttempt.publication_id.asc(),
                    PublicationAttempt.attempt.asc(),
                )
            )
        ).scalars():
            attempts_by_publication[int(attempt.publication_id)].append(attempt)

        items: list[CanonicalPublicationDeliveryInflightAuditItem] = []
        for publication in publications:
            publication_id = int(publication.id)
            attempts = attempts_by_publication.get(publication_id, [])
            schedule = (
                schedules.get(int(publication.schedule_entry_id))
                if publication.schedule_entry_id is not None
                else None
            )
            lease = leases.get(publication_id)
            findings: set[str] = set()

            if publication.legacy_post_task_id is not None:
                if self._canonical_attempt_present(attempts):
                    findings.add(LEGACY_TRANSPORT_RELINKED)
                else:
                    findings.add(LEGACY_TRANSPORT_LINKED)
            if self._has_durable_delivery_evidence(publication, attempts):
                findings.add(DURABLE_DELIVERY_EVIDENCE)
            if self._schedule_mismatch(publication, schedule):
                findings.add(SCHEDULE_MISMATCH)
            if self._attempt_mismatch(publication, attempts):
                findings.add(ATTEMPT_MISMATCH)

            lease_expires_at: datetime | None = None
            if lease is None:
                lease_state = "missing"
                findings.add(MISSING_LEASE)
            else:
                lease_expires_at = _utc(lease.expires_at)
                if lease_expires_at <= current:
                    lease_state = "expired"
                    findings.add(EXPIRED_LEASE)
                else:
                    lease_state = "live"

            ordered_findings = self._ordered_findings(findings)
            classification = (
                ordered_findings[0]
                if ordered_findings
                else HEALTHY_LIVE_LEASE
            )
            items.append(
                CanonicalPublicationDeliveryInflightAuditItem(
                    publication_id=publication_id,
                    classification=classification,
                    findings=ordered_findings,
                    lease_state=lease_state,
                    lease_expires_at=lease_expires_at,
                    legacy_post_task_id=(
                        int(publication.legacy_post_task_id)
                        if publication.legacy_post_task_id is not None
                        else None
                    ),
                    schedule_entry_id=(
                        int(publication.schedule_entry_id)
                        if publication.schedule_entry_id is not None
                        else None
                    ),
                    attempt_count=int(publication.attempt_count or 0),
                )
            )

        return CanonicalPublicationDeliveryInflightAuditPage(
            items=tuple(items),
            next_publication_id=(
                int(publications[-1].id) if not done else None
            ),
            done=done,
        )
