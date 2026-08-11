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


_SILENT_RUNTIME_KEY = "silent"


class CanonicalPublicationDeliveryCapabilityClaimService(
    CanonicalPublicationDeliveryClaimService
):
    """Concrete claim profile for the current plain + explicit silent slice.

    Generic canonical claim remains transport/capability agnostic. This concrete layer
    locks the same proof set first, verifies runtime options are either empty or exactly
    `silent: bool`, then invokes the existing atomic claim while those locks are still
    held. Unknown/effectful runtime keys therefore fail before `queued -> sending`.
    """

    @staticmethod
    def _runtime_supported(options: dict) -> bool:
        if not options:
            return True
        if set(options) != {_SILENT_RUNTIME_KEY}:
            return False
        return type(options.get(_SILENT_RUNTIME_KEY)) is bool

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
        publication, _ = locked

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
        if not self._runtime_supported(options):
            await self.session.rollback()
            return None

        # The underlying claim re-runs planner proof and the remaining restrictions
        # under the same still-open transaction/row locks before committing authority.
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
