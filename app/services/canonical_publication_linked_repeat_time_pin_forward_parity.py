from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Mapping

from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_delivery_planner import CanonicalPublicationDeliveryPlan
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.canonical_publication_linked_repeat_parity import (
    CanonicalPublicationLinkedRepeatParityProof,
    CanonicalPublicationLinkedRepeatParityService,
)


_ALLOWED_KEYS = frozenset(
    {"silent", "pin_on", "forward_to", "autodelete_seconds", "autodelete_report"}
)


def _snapshot(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _legacy_forward_ids(payload: Mapping[str, Any]) -> tuple[int, ...] | None:
    raw = payload.get("forward_to")
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        return None
    result: list[int] = []
    seen: set[int] = set()
    for value in raw:
        parsed = _positive_int(value)
        if parsed is None or parsed in seen:
            return None
        seen.add(parsed)
        result.append(parsed)
    return tuple(result)


class CanonicalPublicationLinkedRepeatTimePinForwardParityService:
    """Read-only exact parity for combined repeat+time+pin+ordered-forward.

    The combined proof is deliberately independent from the narrower time+pin and
    time+forward proofs. It validates both effects together, then strips both from a
    temporary read-only plan/task and delegates the established repeat+time identity
    checks to plain parity. No narrower proof can implicitly authorize this composition.
    """

    def prove(
        self,
        *,
        task: PostTask,
        publication: Publication,
        schedule: ScheduleEntry,
        plan: CanonicalPublicationDeliveryPlan,
    ) -> CanonicalPublicationLinkedRepeatParityProof | None:
        try:
            options = plan.runtime_options()
        except (TypeError, ValueError):
            return None
        if not isinstance(options, dict) or not set(options).issubset(_ALLOWED_KEYS):
            return None
        if options.get("pin_on") is not True or "forward_to" not in options:
            return None

        capability = parse_canonical_publication_delivery_runtime_capability(options)
        if (
            capability is None
            or capability.time_autodelete_seconds is None
            or capability.pin_on is not True
            or not capability.forward_to
            or capability.views_autodelete_requested
        ):
            return None
        if not isinstance(task.payload, Mapping):
            return None
        payload = dict(task.payload)
        if payload.get("pin_on") is not True:
            return None
        legacy_targets = _legacy_forward_ids(payload)
        if legacy_targets is None or legacy_targets != tuple(capability.forward_to):
            return None

        plain_options = dict(options)
        plain_options.pop("pin_on", None)
        plain_options.pop("forward_to", None)
        plain_payload = dict(payload)
        plain_payload.pop("pin_on", None)
        plain_payload.pop("forward_to", None)
        plain_plan = replace(plan, runtime_options_snapshot=_snapshot(plain_options))
        plain_task = SimpleNamespace(
            id=task.id,
            channel_id=task.channel_id,
            scheduled_at=task.scheduled_at,
            error=task.error,
            payload=plain_payload,
        )
        proof = CanonicalPublicationLinkedRepeatParityService().prove(
            task=plain_task,  # type: ignore[arg-type]
            publication=publication,
            schedule=schedule,
            plan=plain_plan,
        )
        if proof is None or proof.time_autodelete_seconds is None:
            return None
        if proof.pin_on or proof.forward_channel_ids or proof.views_autodelete_threshold is not None:
            return None
        return replace(
            proof,
            pin_on=True,
            forward_channel_ids=tuple(int(value) for value in capability.forward_to),
        )
