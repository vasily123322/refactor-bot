from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import PublicationAttempt
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
)
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


def _target_snapshot(value) -> tuple[tuple[int, int], ...] | None:
    if not isinstance(value, list):
        return None
    result: list[tuple[int, int]] = []
    channel_ids: set[int] = set()
    telegram_ids: set[int] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        if set(raw) != {"channel_id", "telegram_chat_id"}:
            return None
        try:
            channel_id = int(raw["channel_id"])
            telegram_chat_id = int(raw["telegram_chat_id"])
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            channel_id <= 0
            or telegram_chat_id == 0
            or channel_id in channel_ids
            or telegram_chat_id in telegram_ids
        ):
            return None
        channel_ids.add(channel_id)
        telegram_ids.add(telegram_chat_id)
        result.append((channel_id, telegram_chat_id))
    return tuple(result)


class CanonicalPublicationDeliveryLivePostActionPlanner:
    """Plan ordered pin/forward actions under the exact live delivery authority.

    Full live lease/intent authorization is reused from the owner/admin planner. For any
    effectful pin/forward capability, attempt #1 must also contain the durable target
    snapshot persisted before the primary provider call. Current Channel resolution must
    still match that immutable snapshot exactly before an action can be reserved.

    The returned deterministic action keys are reservation identities, not retry tokens.
    Their fingerprints include every provider-relevant destination/value so drift after
    reservation becomes suppression/conflict rather than a second provider call.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        *,
        at=None,
    ) -> CanonicalPublicationDeliveryLivePostActionPlan | None:
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

        effectful = bool(capability.pin_on or capability.forward_to)
        if not effectful:
            return CanonicalPublicationDeliveryLivePostActionPlan(
                publication_id=publication_id,
                actions=(),
            )

        attempt = (
            await self.session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                    PublicationAttempt.status == "sending",
                    PublicationAttempt.finished_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if attempt is None or not isinstance(attempt.meta, Mapping):
            return None
        attempt_meta = dict(attempt.meta)
        if attempt_meta.get("canonical_delivery") is not True:
            return None
        snapshot = _target_snapshot(attempt_meta.get(FORWARD_TARGET_SNAPSHOT_META_KEY))
        if snapshot is None:
            return None

        targets = await resolve_canonical_publication_delivery_forward_targets(
            self.session,
            capability,
            lock=False,
        )
        if targets is None:
            return None
        current_targets = tuple(
            (int(target.channel_id), int(target.telegram_chat_id)) for target in targets
        )
        if snapshot != current_targets:
            return None
        if tuple(channel_id for channel_id, _ in snapshot) != capability.forward_to:
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
