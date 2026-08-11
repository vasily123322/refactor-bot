from __future__ import annotations

from datetime import datetime, timezone

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


class CanonicalPublicationDeliveryCapabilityClaimService(
    CanonicalPublicationDeliveryClaimService
):
    """Concrete claim profile for plain/silent/pin/forward canonical delivery.

    Generic canonical claim remains capability agnostic. This layer locks the same
    mutable delivery rows before authority transition, parses the exact supported
    runtime profile, and resolves every forward target while the claim transaction is
    still open. Unsupported intent or target drift therefore fails before
    ``queued -> sending``, attempt creation, lease insertion, or provider execution.
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

        # The underlying claim re-runs canonical planner proof and remaining lifecycle
        # restrictions under the same still-open transaction/row locks before commit.
        return await super().claim(
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
