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
        # An explicit forward profile must contain at least one exact target. Pin may
        # compose with forward because that exact linked parity/replay slice is proven by
        # the parent stage; delete/unknown keys remain independently constrained below.
        return None

    if capability.views_autodelete_requested:
        # Repeat+views is a separate destructive composition. Independent repeat and
        # views availability facts must never compose implicitly into authority. Only a
        # dedicated fact may admit the already-proven plain/silent views slice.
        if not allow_repeat_views:
            return None
        if capability.pin_on or capability.forward_to:
            return None
    elif "autodelete_report" in options:
        # Report has no independent meaning. The generic parser already enforces this,
        # but keep the strict repeat barrier explicit against future parser widening.
        return None

    return deepcopy(options)


class CanonicalPublicationRepeatCapabilityClaimService(
    CanonicalPublicationDeliveryCapabilityClaimService
):
    """Keep repeat authority limited to explicitly proven runtime slices.

    `allow_repeat=True` never removes the nonrepeat barrier for arbitrary understood side
    effects. The established profile admits fixed-delay repeat with optional silent/pin/
    ordered-forward effects that already have dedicated parity/replay proof.

    Plain/silent views autodelete is admitted only when **three** facts are true at the
    claim boundary: repeat continuation authority, a concrete views executor, and the
    dedicated `allow_repeat_views` composition fact. The latter is intentionally default
    off, so independent availability booleans cannot accidentally compose into new
    destructive authority. Views+pin/forward, both time-autodelete keys, dual delete
    modes, unknown runtime keys and unknown repeat semantics remain fail-closed.

    The claim phase only stages indexed views intent atomically with primary authority.
    Actual destructive execution remains post-publication and must pass the locked #287
    gate / #286 lifecycle proof before the reserve-before-DELETE boundary can act.

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
        allow_repeat_views: bool = False,
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
                if (
                    _strict_repeat_runtime_options(
                        plan,
                        allow_repeat_views=bool(
                            allow_repeat_views and allow_views_autodelete
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
