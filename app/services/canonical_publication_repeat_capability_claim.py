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

    if capability.views_autodelete_requested:
        # Repeat+views is a dedicated destructive slice. Do not interpret neutral
        # pin/forward keys as proof of their composition: only plain/silent views
        # intent (+ optional report) belongs to this stage.
        if (
            not allow_repeat_views
            or not set(options).issubset(_REPEAT_VIEWS_RUNTIME_KEYS)
            or "autodelete_views" not in options
            or capability.views_autodelete_threshold is None
            or capability.pin_on
            or capability.forward_to
            or capability.time_autodelete_requested
        ):
            return None
    else:
        # Preserve the already-proven silent/pin/forward surface exactly. Any views
        # key that failed to produce a positive views capability stays fail-closed.
        if not set(options).issubset(_ESTABLISHED_REPEAT_RUNTIME_KEYS):
            return None
        if "forward_to" in options and not capability.forward_to:
            return None

    return deepcopy(options)


class CanonicalPublicationRepeatCapabilityClaimService(
    CanonicalPublicationDeliveryCapabilityClaimService
):
    """Keep repeat authority limited to explicitly proven runtime slices.

    `allow_repeat=True` never removes the nonrepeat barrier for arbitrary understood side
    effects. The established profile admits fixed-delay repeat with optional silent/pin/
    ordered-forward effects that already have dedicated parity/replay proof.

    Plain/silent views autodelete is admitted only when three independent facts are true
    at the claim boundary: repeat continuation authority, concrete views-executor
    availability, and the dedicated `allow_repeat_views` composition fact. The latter is
    default-off and is never inferred from the first two facts.

    This stage deliberately excludes every views+pin/forward composition, including
    neutral pin/forward keys, plus all time-autodelete, dual-delete, unknown runtime and
    unknown repeat semantics. The claim only stages occurrence-local indexed views intent
    atomically with primary authority. Destructive execution remains post-publication and
    still requires the locked repeat-views lifecycle proof plus reserve-before-DELETE.

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
