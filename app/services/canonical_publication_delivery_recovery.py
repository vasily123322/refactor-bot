from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryExpiredLeaseRef,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.scheduler_errors import UNKNOWN_DELIVERY_ERROR


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryRecoveryTick:
    selected: int = 0
    taken_over: int = 0
    failed_unknown: int = 0
    contention: int = 0
    conflicts: int = 0
    failures: int = 0


class CanonicalPublicationDeliveryRecoveryService:
    """Bounded fail-closed recovery for expired canonical Publication leases.

    Recovery never calls Telegram and never retries delivery. It atomically takes an
    expired typed lease, then closes the still-ambiguous ``sending`` lifecycle through
    the exact-token finalizer using the fixed public-safe unknown-delivery error.

    Normal heartbeat cannot revive an expired lease: #206 makes ``renew`` live-only.
    Takeover contention therefore means another recovery owner/token transition won the
    race first, not that an expired delivery worker returned to execution authority.
    """

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _recover_reference(
        self,
        reference: CanonicalPublicationDeliveryExpiredLeaseRef,
        *,
        now: datetime,
    ) -> str:
        async with self.session_factory() as session:
            claims = CanonicalPublicationDeliveryClaimService(session)
            recovery = await claims.take_expired(
                reference,
                holder="canonical-publication-delivery-recovery",
                now=now,
            )
            if recovery is None:
                return "contention"

            result = await CanonicalPublicationDeliveryFinalizer(
                session
            ).complete_failure(
                recovery,
                error=UNKNOWN_DELIVERY_ERROR,
                now=now,
                finished_at=now,
            )
            if result.outcome == "failed":
                return "failed_unknown"
            # A lifecycle/token conflict after takeover is intentionally not repaired
            # and the recovery-owned lease is not deleted here. Its short expiry keeps
            # the occurrence blocked from normal execution while operators/reconciliation
            # inspect the unexpected canonical state.
            return "conflict"

    async def run_once(
        self,
        *,
        batch_size: int = 100,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryRecoveryTick:
        current = _utc(now)
        try:
            limit = max(1, min(int(batch_size), 500))
        except (TypeError, ValueError, OverflowError):
            limit = 100

        async with self.session_factory() as session:
            references = await CanonicalPublicationDeliveryClaimService(session).expired(
                limit=limit,
                now=current,
            )

        counts = {
            "taken_over": 0,
            "failed_unknown": 0,
            "contention": 0,
            "conflicts": 0,
            "failures": 0,
        }
        for reference in references:
            try:
                outcome = await self._recover_reference(reference, now=current)
            except Exception as exc:
                counts["failures"] += 1
                logger.warning(
                    "Canonical publication delivery recovery candidate failed "
                    "publication_id={} error_type={}",
                    int(reference.publication_id),
                    type(exc).__name__,
                )
                continue

            if outcome != "contention":
                counts["taken_over"] += 1
            if outcome == "failed_unknown":
                counts["failed_unknown"] += 1
            elif outcome == "contention":
                counts["contention"] += 1
            else:
                counts["conflicts"] += 1

        return CanonicalPublicationDeliveryRecoveryTick(
            selected=len(references),
            **counts,
        )
