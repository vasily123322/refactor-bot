from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_claim import CanonicalPublicationDeliveryClaim
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlanner
from app.services.canonical_publication_legacy_transport_handoff import (
    _supported_runtime_options,
)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _strict_fixed_delay_repeat(plan) -> bool:
    try:
        rule = plan.repeat_rule()
    except (TypeError, ValueError):
        return False
    if not isinstance(rule, Mapping):
        return False
    normalized = dict(rule)
    return (
        set(normalized).issubset({"enabled", "seconds"})
        and normalized.get("enabled") is True
        and _positive_int(normalized.get("seconds")) is not None
    )


class CanonicalPublicationRepeatCapabilityClaimService(
    CanonicalPublicationDeliveryCapabilityClaimService
):
    """Keep repeat authority limited to the first proven runtime slice.

    `allow_repeat=True` must not mean "remove the nonrepeat barrier for every currently
    understood side effect". Repeat post-send parity is not yet proven for pin/forward,
    views, and especially time-autodelete (legacy may align a timer to the repeat cadence).

    This layer therefore admits repeat only when:
      * fixed-delay rule is exact and positive;
      * runtime is empty or explicit `silent: bool` only.

    Non-repeat rows retain the complete existing capability surface. The proof is taken
    while the same mutable delivery rows are locked; the parent service then re-locks and
    re-proves before authority commit, so drift cannot widen the profile between checks.
    """

    async def claim_supported(
        self,
        *,
        publication_id: int,
        holder: str,
        ttl_seconds: int,
        now: datetime | None = None,
        allow_time_autodelete: bool = False,
        allow_views_autodelete: bool = False,
        allow_repeat: bool = False,
    ) -> CanonicalPublicationDeliveryClaim | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        if allow_repeat:
            locked = await self._lock_delivery_rows(safe_publication_id)
            if locked is None:
                await self.session.rollback()
                return None
            plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
                safe_publication_id,
                at=now,
            )
            if plan is None:
                await self.session.rollback()
                return None
            try:
                rule = plan.repeat_rule()
            except (TypeError, ValueError):
                await self.session.rollback()
                return None
            repeat_enabled = isinstance(rule, Mapping) and rule.get("enabled") is True
            if repeat_enabled:
                if not _strict_fixed_delay_repeat(plan):
                    await self.session.rollback()
                    return None
                if _supported_runtime_options(plan) is None:
                    await self.session.rollback()
                    return None

        return await super().claim_supported(
            publication_id=safe_publication_id,
            holder=holder,
            ttl_seconds=ttl_seconds,
            now=now,
            allow_time_autodelete=allow_time_autodelete,
            allow_views_autodelete=allow_views_autodelete,
            allow_repeat=allow_repeat,
        )
