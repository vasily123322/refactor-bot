from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlanner,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
    resolve_canonical_publication_delivery_forward_targets,
)
from app.services.telegram_results import normalize_telegram_message_ids


@dataclass(frozen=True, slots=True)
class CanonicalPublicationLivePostAction:
    publication_id: int
    action_key: str
    action_type: str
    intent_fingerprint: str
    source_telegram_chat_id: int
    source_message_id: int
    target_channel_id: int | None = None
    target_telegram_chat_id: int | None = None
    disable_notification: bool = False


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryLivePostActionPlan:
    publication_id: int
    actions: tuple[CanonicalPublicationLivePostAction, ...]


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CanonicalPublicationDeliveryLivePostActionPlanner:
    """Plan ordered pin/forward actions under the exact live delivery authority.

    The existing live auxiliary planner is intentionally reused as the full immutable
    intent/lease/lifecycle authorization proof. Only after that proof succeeds do we
    parse the current supported runtime capability and resolve forward Channel IDs to
    their current Telegram destinations.

    The returned deterministic action keys are reservation identities, not retry tokens.
    Their fingerprints include every provider-relevant destination/value so target drift
    after a reservation becomes a ledger conflict rather than a second provider call.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        *,
        at=None,
    ) -> CanonicalPublicationDeliveryLivePostActionPlan | None:
        # Full exact lease + intent reauthorization, independent of whether owner/admin
        # actions themselves are configured.
        authorized = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
            self.session
        ).plan(context, at=at)
        if authorized is None:
            return None

        try:
            runtime_options = context.plan.runtime_options()
            publication_id = int(context.publication_id)
            source_chat_id = int(context.plan.telegram_chat_id)
        except (TypeError, ValueError, OverflowError):
            return None
        capability = parse_canonical_publication_delivery_runtime_capability(
            runtime_options
        )
        if capability is None:
            return None

        message_ids = normalize_telegram_message_ids(context.message_ids)
        if not message_ids:
            return None
        targets = await resolve_canonical_publication_delivery_forward_targets(
            self.session,
            capability,
            lock=False,
        )
        if targets is None:
            return None

        actions: list[CanonicalPublicationLivePostAction] = []
        if capability.pin_on:
            message_id = int(message_ids[-1])
            payload = {
                "publication_id": publication_id,
                "action_type": "pin",
                "source_telegram_chat_id": source_chat_id,
                "source_message_id": message_id,
            }
            actions.append(
                CanonicalPublicationLivePostAction(
                    publication_id=publication_id,
                    action_key=f"pin:{message_id}",
                    action_type="pin",
                    intent_fingerprint=_fingerprint(payload),
                    source_telegram_chat_id=source_chat_id,
                    source_message_id=message_id,
                )
            )

        for target in targets:
            for message_id in message_ids:
                payload = {
                    "publication_id": publication_id,
                    "action_type": "forward",
                    "source_telegram_chat_id": source_chat_id,
                    "source_message_id": int(message_id),
                    "target_channel_id": int(target.channel_id),
                    "target_telegram_chat_id": int(target.telegram_chat_id),
                    "disable_notification": capability.forward_silent,
                }
                actions.append(
                    CanonicalPublicationLivePostAction(
                        publication_id=publication_id,
                        action_key=(
                            f"forward:{int(target.channel_id)}:{int(message_id)}"
                        ),
                        action_type="forward",
                        intent_fingerprint=_fingerprint(payload),
                        source_telegram_chat_id=source_chat_id,
                        source_message_id=int(message_id),
                        target_channel_id=int(target.channel_id),
                        target_telegram_chat_id=int(target.telegram_chat_id),
                        disable_notification=capability.forward_silent,
                    )
                )

        return CanonicalPublicationDeliveryLivePostActionPlan(
            publication_id=publication_id,
            actions=tuple(actions),
        )
