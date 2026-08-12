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
    {"silent", "pin_on", "autodelete_seconds", "autodelete_report"}
)


def _snapshot(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class CanonicalPublicationLinkedRepeatTimePinParityService:
    """Read-only exact parity for the repeat+time+pin composition.

    The established plain repeat+time proof remains unchanged and continues to reject
    pin. This composition proof strips only the already-proven pin bit into a temporary
    read-only view, delegates every other repeat/time/legacy identity check to the plain
    parity service, then restores `pin_on=True` in the returned immutable proof.

    No claim, transport retirement, timer runtime or Telegram authority is created here.
    Forward and views remain outside this composition.
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
        if options.get("pin_on") is not True:
            return None

        capability = parse_canonical_publication_delivery_runtime_capability(options)
        if (
            capability is None
            or capability.time_autodelete_seconds is None
            or capability.pin_on is not True
            or capability.forward_to
            or capability.views_autodelete_requested
        ):
            return None

        if not isinstance(task.payload, Mapping):
            return None
        payload = dict(task.payload)
        if payload.get("pin_on") is not True:
            return None

        plain_options = dict(options)
        plain_options.pop("pin_on", None)
        plain_payload = dict(payload)
        plain_payload.pop("pin_on", None)

        plain_plan = replace(
            plan,
            runtime_options_snapshot=_snapshot(plain_options),
        )
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
        if (
            proof.pin_on
            or proof.forward_channel_ids
            or proof.views_autodelete_threshold is not None
        ):
            return None

        return replace(proof, pin_on=True)
