from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol


class _RepeatRulePlan(Protocol):
    def repeat_rule(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationOwnerNotificationDecision:
    outcome: Literal["notify", "suppress", "invalid"]
    owner_notice_allowed: bool
    repeat_enabled: bool
    repeat_seconds: int | None = None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


class CanonicalPublicationOwnerNotificationPolicy:
    """Decide whether the canonical live hook may send the owner-published notice.

    Historical Scheduler behavior suppresses the owner publication notice for repeat
    occurrences. Canonical delivery must preserve that distinction before repeat
    authority is enabled.

    The policy is deliberately fail-closed. Only an exact non-repeat rule permits the
    notice. A well-formed enabled fixed-delay repeat suppresses it, and malformed or
    future/unknown repeat semantics are classified invalid and also suppress the notice.
    Provider code can therefore use only ``owner_notice_allowed`` without accidentally
    widening notification behavior when repeat semantics evolve.
    """

    @staticmethod
    def decide(
        plan: _RepeatRulePlan,
    ) -> CanonicalPublicationOwnerNotificationDecision:
        try:
            raw_rule = plan.repeat_rule()
        except (TypeError, ValueError, OverflowError, AttributeError):
            return CanonicalPublicationOwnerNotificationDecision(
                outcome="invalid",
                owner_notice_allowed=False,
                repeat_enabled=False,
            )
        if not isinstance(raw_rule, Mapping):
            return CanonicalPublicationOwnerNotificationDecision(
                outcome="invalid",
                owner_notice_allowed=False,
                repeat_enabled=False,
            )

        rule = dict(raw_rule)
        if not set(rule).issubset({"enabled", "seconds"}):
            return CanonicalPublicationOwnerNotificationDecision(
                outcome="invalid",
                owner_notice_allowed=False,
                repeat_enabled=False,
            )

        enabled = rule.get("enabled")
        if enabled in (None, False):
            # Disabled repeat must not carry an active cadence. Neutral/missing seconds
            # remain compatible with historical non-repeat behavior.
            if rule.get("seconds") not in (None, False, 0, "0", ""):
                return CanonicalPublicationOwnerNotificationDecision(
                    outcome="invalid",
                    owner_notice_allowed=False,
                    repeat_enabled=False,
                )
            return CanonicalPublicationOwnerNotificationDecision(
                outcome="notify",
                owner_notice_allowed=True,
                repeat_enabled=False,
            )

        if enabled is not True:
            return CanonicalPublicationOwnerNotificationDecision(
                outcome="invalid",
                owner_notice_allowed=False,
                repeat_enabled=False,
            )
        seconds = _positive_int(rule.get("seconds"))
        if seconds is None:
            return CanonicalPublicationOwnerNotificationDecision(
                outcome="invalid",
                owner_notice_allowed=False,
                repeat_enabled=True,
            )
        return CanonicalPublicationOwnerNotificationDecision(
            outcome="suppress",
            owner_notice_allowed=False,
            repeat_enabled=True,
            repeat_seconds=seconds,
        )
