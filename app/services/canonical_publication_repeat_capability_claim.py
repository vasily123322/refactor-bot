from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_claim import CanonicalPublicationDeliveryClaim
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlanner
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)


_REPEAT_RUNTIME_KEYS = frozenset(
    {"silent", "pin_on", "forward_to", "autodelete_views", "autodelete_report"}
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


def _strict_repeat_runtime_options(
    plan,
    *,
    allow_repeat_views: bool = False,
    allow_repeat_views_pin: bool = False,
) -> dict[str, Any] | None:
    try:
        options = plan.runtime_options()
    except (TypeError, ValueError):
        return None
    if not isinstance(options, dict) or not set(options).issubset(_REPEAT_RUNTIME_KEYS):
        return None

    capability = parse_canonical_publication_delivery_runtime_capability(options)
    if capability is None:
        return None

    if "forward_to" in options and not capability.forward_to:
        return None

    if capability.views_autodelete_requested:
        if not allow_repeat_views:
            return None
        if capability.forward_to:
            return None
        if capability.pin_on and not allow_repeat_views_pin:
            return None
    elif "autodelete_report" in options:
        return None

    return deepcopy(options)


class CanonicalPublicationRepeatCapabilityClaimService(
    CanonicalPublicationDeliveryCapabilityClaimService
):
    """Keep repeat authority limited to explicitly proven runtime slices.

    Base repeat, pin/forward and plain repeat+views retain their existing independent
    gates. Views+pin is now recognized by the strict pre-send claim only behind the
    additional `allow_repeat_views_pin` composition fact. That fact defaults off and is
    not inferred from `allow_repeat_views`, pin support, or dependency availability.

    Views+forward, every time-autodelete key, dual delete modes, unknown runtime keys and
    unknown repeat-rule semantics remain fail-closed. This phase stages only occurrence-
    local views intent with primary authority; post-publication DELETE still needs the
    separately gated lifecycle/destructive composition.
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
        allow_repeat_views: bool = False,
        allow_repeat_views_pin: bool = False,
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
                repeat_views_enabled = bool(
                    allow_repeat_views and allow_views_autodelete
                )
                if (
                    _strict_repeat_runtime_options(
                        plan,
                        allow_repeat_views=repeat_views_enabled,
                        allow_repeat_views_pin=bool(
                            allow_repeat_views_pin and repeat_views_enabled
                        ),
                    )
                    is None
                ):
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