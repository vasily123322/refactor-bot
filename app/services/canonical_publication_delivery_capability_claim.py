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
    """Concrete claim profile for plain/silent/pin/forward canonical delivery.

    Generic canonical claim remains capability agnostic. This layer locks the same
    mutable delivery rows before authority transition, parses the exact supported
    runtime profile, and resolves every forward target while the claim transaction is
    still open. Unsupported intent or target drift therefore fails before
    ``queued -> sending``, attempt creation, lease insertion, or provider execution.

    Resolved forward destinations are then persisted on canonical attempt #1 before the
    provider may run. The delivery lease is re-proven live after that durable snapshot,
    closing the commit-to-provider TTL window and giving post-send actions immutable
    destination evidence that survives process restarts.
    """

    async def claim_supported(
        self,
        *,
        publication_id: int,
        holder: str,
        ttl_seconds: int,
        now: datetime | None = None,
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

        # Resolve and lock target rows before primary authority is committed. Missing
        # targets or duplicate Telegram destinations must not become post-send surprises.
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

        # The underlying claim re-runs canonical planner proof and remaining lifecycle
        # restrictions under the same still-open transaction/row locks before commit.
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

        # Claim commit has already established `sending + attempt + lease`. Persist the
        # resolved destination snapshot before any provider call. Failure here is
        # intentionally fail-closed: the caller gets no executable claim, while the
        # durable sending/lease state remains an explicit recovery barrier.
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

        # In production use a fresh wall-clock instant after the snapshot commit. Tests
        # that explicitly supply `now` retain deterministic clock semantics.
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
