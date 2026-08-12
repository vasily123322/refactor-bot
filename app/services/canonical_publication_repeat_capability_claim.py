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
_REPEAT_RUNTIME_KEYS = _ESTABLISHED_REPEAT_RUNTIME_KEYS | _REPEAT_VIEWS_RUNTIME_KEYS
_REPEAT_TIME_RUNTIME_KEYS = frozenset(
    {"autodelete_seconds", "autodelete_effective_seconds"}
)
_REPEAT_TIME_QUEUE_RUNTIME_KEYS = frozenset(
    {"silent", "autodelete_seconds", "autodelete_report"}
)
_REPEAT_TIME_PIN_RUNTIME_KEYS = _REPEAT_TIME_QUEUE_RUNTIME_KEYS | frozenset({"pin_on"})


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
    allow_repeat_time: bool = False,
    allow_repeat_time_pin: bool = False,
    allow_repeat_views: bool = False,
    allow_repeat_views_pin: bool = False,
    allow_repeat_views_forward: bool = False,
    allow_repeat_views_pin_forward: bool = False,
) -> dict[str, Any] | None:
    try:
        options = plan.runtime_options()
    except (TypeError, ValueError):
        return None
    if not isinstance(options, dict):
        return None

    has_repeat_time_key = any(key in options for key in _REPEAT_TIME_RUNTIME_KEYS)
    if has_repeat_time_key:
        # Queue-time repeat+time is intentionally its own narrow family. Generated
        # `autodelete_effective_seconds` is execution state, never caller intent.
        if "autodelete_effective_seconds" in options or not allow_repeat_time:
            return None

        capability = parse_canonical_publication_delivery_runtime_capability(options)
        if (
            capability is None
            or "autodelete_seconds" not in options
            or capability.time_autodelete_seconds is None
            or capability.views_autodelete_requested
            or capability.forward_to
        ):
            return None

        has_pin_key = "pin_on" in options
        if has_pin_key:
            if (
                not allow_repeat_time_pin
                or capability.pin_on is not True
                or not set(options).issubset(_REPEAT_TIME_PIN_RUNTIME_KEYS)
            ):
                return None
        else:
            if capability.pin_on:
                return None
            if not set(options).issubset(_REPEAT_TIME_QUEUE_RUNTIME_KEYS):
                return None
        return deepcopy(options)

    if not set(options).issubset(_REPEAT_RUNTIME_KEYS):
        return None

    capability = parse_canonical_publication_delivery_runtime_capability(options)
    if capability is None:
        return None

    if capability.views_autodelete_requested:
        if not allow_repeat_views:
            return None

        has_pin_key = "pin_on" in options
        has_forward_key = "forward_to" in options
        has_combined_keys = has_pin_key and has_forward_key

        if has_combined_keys:
            # Exact combined composition requires all underlying facts plus the dedicated
            # combined bit. Neutral pin/forward keys are not accepted as combined intent.
            if not (
                allow_repeat_views_pin_forward
                and allow_repeat_views_pin
                and allow_repeat_views_forward
                and capability.pin_on
                and capability.forward_to
            ):
                return None
            allowed_views_keys = _REPEAT_VIEWS_RUNTIME_KEYS | frozenset(
                {"pin_on", "forward_to"}
            )
        else:
            allowed_views_keys = _REPEAT_VIEWS_RUNTIME_KEYS
            if allow_repeat_views_pin:
                allowed_views_keys = allowed_views_keys | frozenset({"pin_on"})
            if allow_repeat_views_forward:
                allowed_views_keys = allowed_views_keys | frozenset({"forward_to"})

        if (
            not set(options).issubset(allowed_views_keys)
            or "autodelete_views" not in options
            or capability.views_autodelete_threshold is None
            or capability.time_autodelete_requested
        ):
            return None
        if has_pin_key and not capability.pin_on:
            return None
        if has_pin_key and not allow_repeat_views_pin:
            return None
        if has_forward_key:
            if not allow_repeat_views_forward or not capability.forward_to:
                return None
        elif capability.forward_to:
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
    """Keep repeat authority limited to explicit independently proven compositions.

    Plain repeat+time requires generic destructive-time plus `allow_repeat_time`.
    Repeat+time+pin is narrower again and additionally requires the independent
    `allow_repeat_time_pin` fact; plain time authority cannot imply pin composition.
    Generated timer state, forward, views and unknown effects remain closed.

    Plain repeat+views, views+pin and views+forward retain separate default-off facts.
    Views+pin+forward is narrower again and requires the complete underlying repeat/views,
    pin and forward fact set plus `allow_repeat_views_pin_forward`. Independent pin and
    forward facts can never implicitly compose into combined authority.
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
        allow_repeat_time: bool = False,
        allow_repeat_time_pin: bool = False,
        allow_repeat_views: bool = False,
        allow_repeat_views_pin: bool = False,
        allow_repeat_views_forward: bool = False,
        allow_repeat_views_pin_forward: bool = False,
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
                repeat_time_enabled = bool(
                    allow_repeat_time and allow_time_autodelete
                )
                repeat_time_pin_enabled = bool(
                    allow_repeat_time_pin and repeat_time_enabled
                )
                repeat_views_enabled = bool(
                    allow_repeat_views and allow_views_autodelete
                )
                pin_enabled = bool(allow_repeat_views_pin and repeat_views_enabled)
                forward_enabled = bool(
                    allow_repeat_views_forward and repeat_views_enabled
                )
                combined_enabled = bool(
                    allow_repeat_views_pin_forward
                    and pin_enabled
                    and forward_enabled
                )
                if (
                    _strict_repeat_runtime_options(
                        plan,
                        allow_repeat_time=repeat_time_enabled,
                        allow_repeat_time_pin=repeat_time_pin_enabled,
                        allow_repeat_views=repeat_views_enabled,
                        allow_repeat_views_pin=pin_enabled,
                        allow_repeat_views_forward=forward_enabled,
                        allow_repeat_views_pin_forward=combined_enabled,
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
