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

    def __init__(
        self,
        *args,
        allow_repeat_time: bool = False,
        allow_repeat_time_pin: bool = False,
        allow_repeat_time_forward: bool = False,
        allow_repeat_views: bool = False,
        allow_repeat_views_pin: bool = False,
        allow_repeat_views_forward: bool = False,
        allow_repeat_views_pin_forward: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.allow_repeat_time = bool(allow_repeat_time)
        self.allow_repeat_time_pin = bool(allow_repeat_time_pin)
        self.allow_repeat_time_forward = bool(allow_repeat_time_forward)
        self.allow_repeat_views = bool(allow_repeat_views)
        self.allow_repeat_views_pin = bool(allow_repeat_views_pin)
        self.allow_repeat_views_forward = bool(allow_repeat_views_forward)
        self.allow_repeat_views_pin_forward = bool(allow_repeat_views_pin_forward)

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
                allow_repeat_time=self.allow_repeat_time,
                allow_repeat_time_pin=self.allow_repeat_time_pin,
                allow_repeat_time_forward=self.allow_repeat_time_forward,
                allow_repeat_views=self.allow_repeat_views,
                allow_repeat_views_pin=self.allow_repeat_views_pin,
                allow_repeat_views_forward=self.allow_repeat_views_forward,
                allow_repeat_views_pin_forward=self.allow_repeat_views_pin_forward,
            )
