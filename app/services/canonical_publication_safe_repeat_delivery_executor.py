from __future__ import annotations

from datetime import datetime

from app.services.canonical_publication_delivery_claim import CanonicalPublicationDeliveryClaim
from app.services.canonical_publication_repeat_delivery_executor import (
    CanonicalPublicationRepeatDeliveryExecutor,
)
from app.services.canonical_publication_repeat_capability_claim import (
    CanonicalPublicationRepeatCapabilityClaimService,
)


class CanonicalPublicationSafeRepeatDeliveryExecutor(
    CanonicalPublicationRepeatDeliveryExecutor
):
    """Repeat-gated executor that keeps effectful repeat profiles fail-closed."""

    async def _claim(
        self,
        publication_id: int,
        *,
        now: datetime | None,
    ) -> CanonicalPublicationDeliveryClaim | None:
        async with self.session_factory() as session:
            return await CanonicalPublicationRepeatCapabilityClaimService(
                session
            ).claim_supported(
                publication_id=int(publication_id),
                holder=self.holder,
                ttl_seconds=self.lease_seconds,
                now=now,
                allow_time_autodelete=self.allow_time_autodelete,
                allow_views_autodelete=self.allow_views_autodelete,
                allow_repeat=self.allow_repeat,
            )
