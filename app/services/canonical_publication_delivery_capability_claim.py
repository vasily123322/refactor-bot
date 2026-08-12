from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.domain.publishing.models import PublicationAttempt
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
    CanonicalPublicationDeliveryClaimRequirements,
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
    resolve_canonical_publication_delivery_forward_targets,
)


FORWARD_TARGET_SNAPSHOT_META_KEY = "canonical_forward_targets"


class CanonicalPublicationDeliveryCapabilityClaimService(
    CanonicalPublicationDeliveryClaimService
):
    """Concrete claim profile for the explicitly enabled canonical delivery capabilities.

    Generic canonical claim remains capability agnostic. This layer locks the mutable
    delivery proof rows before authority transition, parses the exact supported runtime
    profile, and resolves forward targets while the claim transaction is open.

    Time-based autodelete is additionally guarded by an explicit executor-availability
    flag. The default is disabled so requested timer semantics can never be silently
    accepted in a runtime where the canonical delete worker is unavailable.
    """

    async def claim_supported(
        self,
        *,
        publication_id: int,
        holder: str,
        ttl_seconds: int,
        now: datetime | None = None,
        allow_time_autodelete: bool = False,
    ) -> CanonicalPublicationDeliveryClaim | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        current = now or datetime.now(timezone.utc)
        locked = await self._lock_delivery_rows(safe_publication_id)
        if locked is None:
            await self.session.rollback()
            return None

        plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
            safe_publication_id,
            at=current,
        )
        if plan is None:
            await self.session.rollback()
            return None
        try:
            options = plan.runtime_options()
        except (TypeError, ValueError):
            await self.session.rollback()
            return None
        capability = parse_canonical_publication_delivery_runtime_capability(options)
        if capability is None:
            await self.session.rollback()
            return None
        if capability.time_autodelete_requested and not allow_time_autodelete:
            await self.session.rollback()
            return None

        targets = await resolve_canonical_publication_delivery_forward_targets(
            self.session,
            capability,
            lock=True,
        )
        if targets is None:
            await self.session.rollback()
            return None
        target_snapshot = [
            {
                "channel_id": int(target.channel_id),
                "telegram_chat_id": int(target.telegram_chat_id),
            }
            for target in targets
        ]

        claim = await super().claim(
            publication_id=safe_publication_id,
            holder=holder,
            ttl_seconds=ttl_seconds,
            now=current,
            requirements=CanonicalPublicationDeliveryClaimRequirements(
                require_empty_runtime_options=False,
                require_nonrepeat=True,
                require_transport_retired=True,
            ),
        )
        if claim is None:
            return None

        # Persist all resolved forward destinations before any provider call. Failure
        # after the authority transition intentionally leaves `sending + lease` for
        # recovery and returns no executable claim.
        try:
            attempt = (
                await self.session.execute(
                    select(PublicationAttempt).where(
                        PublicationAttempt.publication_id == safe_publication_id,
                        PublicationAttempt.attempt == 1,
                        PublicationAttempt.status == "sending",
                        PublicationAttempt.finished_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if attempt is None:
                await self.session.rollback()
                return None
            attempt_meta = dict(attempt.meta or {})
            if attempt_meta.get("canonical_delivery") is not True:
                await self.session.rollback()
                return None
            attempt_meta[FORWARD_TARGET_SNAPSHOT_META_KEY] = target_snapshot
            attempt.meta = attempt_meta
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        # Re-prove the exact lease live after the snapshot commit before returning an
        # executable claim. Production uses a fresh wall clock; explicit test clocks stay
        # deterministic.
        renew_at = current if now is not None else None
        renewed = await super().renew(
            claim.lease,
            ttl_seconds=ttl_seconds,
            now=renew_at,
        )
        if renewed is None:
            return None

        return CanonicalPublicationDeliveryClaim(
            plan=claim.plan,
            lease=renewed,
            attempt=claim.attempt,
        )
