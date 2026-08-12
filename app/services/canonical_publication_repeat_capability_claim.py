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


_ESTABLISHED_REPEAT_RUNTIME_KEYS = frozenset({"silent", "pin_on", "forward_to"})
_REPEAT_VIEWS_RUNTIME_KEYS = frozenset(
    {"silent", "autodelete_views", "autodelete_report"}
)
_REPEAT_VIEWS_PIN_RUNTIME_KEYS = _REPEAT_VIEWS_RUNTIME_KEYS | frozenset({"pin_on"})
_REPEAT_RUNTIME_KEYS = _ESTABLISHED_REPEAT_RUNTIME_KEYS | _REPEAT_VIEWS_RUNTIME_KEYS


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

    if capability.views_autodelete_requested:
        # Keep the stronger #301 key-shape barrier. A views profile may contain pin_on
        # only after the narrower composition fact is explicit; forward/time stay closed.
        allowed_views_keys = (
            _REPEAT_VIEWS_PIN_RUNTIME_KEYS
            if allow_repeat_views_pin
            else _REPEAT_VIEWS_RUNTIME_KEYS
        )
        if (
            not allow_repeat_views
            or not set(options).issubset(allowed_views_keys)
            or "autodelete_views" not in options
            or capability.views_autodelete_threshold is None
            or capability.forward_to
            or capability.time_autodelete_requested
            or (capability.pin_on and not allow_repeat_views_pin)
        ):
            return None
    else:
        if not set(options).issubset(_ESTABLISHED_REPEAT_RUNTIME_KEYS):
            return None
        if "forward_to" in options and not capability.forward_to:
            return None

    return deepcopy(options)


class CanonicalPublicationRepeatCapabilityClaimService(
    CanonicalPublicationDeliveryCapabilityClaimService
):
    """Keep repeat authority limited to explicitly proven runtime slices.

    Established repeat and plain repeat+views preserve their existing independent gates.
    Views+pin is narrower again: it requires the ordinary repeat fact, concrete views
    executor availability, the dedicated repeat+views fact, and a separate default-off
    repeat+views+pin composition fact. Neutral pin keys are not admitted into the views
    slice unless that narrower fact is present.

    Views+forward, every time-autodelete key, dual-delete, unknown runtime keys and unknown
    repeat semantics remain fail-closed. This phase only stages occurrence-local indexed
    views intent with primary authority; post-publication DELETE still requires the locked
    lifecycle and reserve-before-provider destructive proof.
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
