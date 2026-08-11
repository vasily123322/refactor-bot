from __future__ import annotations

from datetime import datetime

from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_claim import CanonicalPublicationDeliveryClaim
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)


class CanonicalPublicationRepeatDeliveryExecutor(CanonicalPublicationDeliveryExecutor):
    """Canonical delivery executor with an independent repeat-continuation gate.

    Provider/heartbeat/post-send/finalization behavior is inherited unchanged. Only claim
    acquisition is specialized so repeat authority can remain default-off and depend on
    a successfully started continuation worker.
    """

    def __init__(self, *args, allow_repeat: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.allow_repeat = bool(allow_repeat)

    async def _claim(
        self,
        publication_id: int,
        *,
        now: datetime | None,
    ) -> CanonicalPublicationDeliveryClaim | None:
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryCapabilityClaimService(
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
