from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import ScheduleEntry
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
    CanonicalPublicationDeliveryPlanner,
)


_NEUTRAL_BOOLEAN_OPTIONS = frozenset({"silent", "pin_on", "autodelete_report"})
_NEUTRAL_INTEGER_OPTIONS = frozenset(
    {
        "autodelete_seconds",
        "autodelete_effective_seconds",
        "autodelete_views",
    }
)
_ALLOWED_OPTIONS = _NEUTRAL_BOOLEAN_OPTIONS | _NEUTRAL_INTEGER_OPTIONS | {"forward_to"}


def _neutral_boolean(value: Any) -> bool:
    """Match legacy ``bool(payload.get(key, False))`` without accepting odd truthy input."""

    if value is None or value is False or value == "":
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    # In particular, the string "0" is truthy in the legacy scheduler and therefore
    # effectful for silent/pin/report despite looking numerically false.
    return False


def _neutral_integer(value: Any) -> bool:
    if value in (None, False, ""):
        return True
    if isinstance(value, bool):
        return value is False
    try:
        return int(value) == 0
    except (TypeError, ValueError, OverflowError):
        return False


def _neutral_forward_targets(value: Any) -> bool:
    if value in (None, False, ""):
        return True
    return isinstance(value, (list, tuple)) and len(value) == 0


def _primary_only_runtime(options: Mapping[str, Any]) -> bool:
    for key, value in options.items():
        if key not in _ALLOWED_OPTIONS:
            return False
        if key in _NEUTRAL_BOOLEAN_OPTIONS and not _neutral_boolean(value):
            return False
        if key in _NEUTRAL_INTEGER_OPTIONS and not _neutral_integer(value):
            return False
        if key == "forward_to" and not _neutral_forward_targets(value):
            return False
    return True


def _safe_nonrepeat(rule: Any) -> bool:
    if rule is None:
        return True
    if not isinstance(rule, Mapping):
        return False
    normalized = {str(key): value for key, value in rule.items()}
    if set(normalized) - {"enabled", "seconds"}:
        return False
    if normalized.get("enabled") not in (None, False, 0):
        return False
    seconds = normalized.get("seconds")
    if seconds in (None, False, 0, "", "0"):
        return True
    return False


@dataclass(frozen=True, slots=True)
class CanonicalPublicationPrimaryDeliveryCapability:
    delivery: CanonicalPublicationDeliveryPlan


class CanonicalPublicationPrimaryDeliveryCapabilityPlanner:
    """Prove the conservative first canonical executor cutover slice.

    Primary document delivery is already transport-independent, but several historical
    runtime options still imply secondary side effects outside ``send_document``. Until
    those semantics have canonical executors, this planner permits only non-repeat
    Publications whose runtime options are explicitly neutral. Unknown options fail
    closed instead of being silently ignored.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        publication_id: int,
        *,
        at: datetime | None = None,
    ) -> CanonicalPublicationPrimaryDeliveryCapability | None:
        delivery = await CanonicalPublicationDeliveryPlanner(self.session).plan(
            publication_id,
            at=at,
        )
        if delivery is None:
            return None
        options = delivery.runtime_options()
        if not _primary_only_runtime(options):
            return None

        schedule = await self.session.get(ScheduleEntry, int(delivery.schedule_entry_id))
        if (
            schedule is None
            or schedule.status != "pending"
            or int(schedule.channel_id) != int(delivery.channel_id)
            or int(schedule.content_item_id) != int(delivery.content_item_id)
            or int(schedule.content_revision) != int(delivery.content_revision)
            or not _safe_nonrepeat(schedule.repeat_rule)
        ):
            return None
        return CanonicalPublicationPrimaryDeliveryCapability(delivery=delivery)
