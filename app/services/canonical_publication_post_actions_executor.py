from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from loguru import logger

from app.services.canonical_publication_post_actions_planner import (
    CanonicalPublicationPostDeliveryActionPlan,
)


class CanonicalPostDeliveryBot(Protocol):
    async def pin_chat_message(self, *, chat_id: int, message_id: int): ...

    async def forward_message(
        self,
        *,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        disable_notification: bool,
    ): ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationPostDeliveryActionExecution:
    publication_id: int
    pin_requested: int = 0
    pin_succeeded: int = 0
    pin_failed: int = 0
    forward_requested: int = 0
    forward_succeeded: int = 0
    forward_failed: int = 0


class CanonicalPublicationPostDeliveryActionExecutor:
    """Run one proven pin/forward plan with best-effort failure semantics.

    The input is an immutable read-only plan, so this executor performs no database
    reads or writes and cannot change the already terminal Publication outcome.
    Provider exceptions are logged by type only and counted instead of persisted.
    Cancellation remains authoritative and is never swallowed.

    This is deliberately a one-shot primitive, not a retryable worker API. Forwarding
    is not idempotent: replaying the same plan may create duplicate forwarded messages.
    Any runtime coordinator must therefore invoke it only inside an execution boundary
    that cannot retry the plan, or first add durable per-action completion identity.
    """

    def __init__(self, bot: CanonicalPostDeliveryBot) -> None:
        self.bot = bot

    async def execute(
        self,
        plan: CanonicalPublicationPostDeliveryActionPlan,
    ) -> CanonicalPublicationPostDeliveryActionExecution:
        pin_requested = 1 if plan.pin_last_message else 0
        pin_succeeded = 0
        pin_failed = 0
        if plan.pin_last_message:
            try:
                await self.bot.pin_chat_message(
                    chat_id=int(plan.source_telegram_chat_id),
                    message_id=int(plan.message_ids[-1]),
                )
                pin_succeeded = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                pin_failed = 1
                logger.warning(
                    "Canonical post-delivery pin failed publication_id={} error_type={}",
                    int(plan.publication_id),
                    type(exc).__name__,
                )

        forward_requested = len(plan.forward_targets) * len(plan.message_ids)
        forward_succeeded = 0
        forward_failed = 0
        for target in plan.forward_targets:
            for message_id in plan.message_ids:
                try:
                    await self.bot.forward_message(
                        chat_id=int(target.telegram_chat_id),
                        from_chat_id=int(plan.source_telegram_chat_id),
                        message_id=int(message_id),
                        disable_notification=bool(plan.forward_silent),
                    )
                    forward_succeeded += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    forward_failed += 1
                    logger.warning(
                        "Canonical post-delivery forward failed publication_id={} "
                        "target_channel_id={} error_type={}",
                        int(plan.publication_id),
                        int(target.channel_id),
                        type(exc).__name__,
                    )

        return CanonicalPublicationPostDeliveryActionExecution(
            publication_id=int(plan.publication_id),
            pin_requested=pin_requested,
            pin_succeeded=pin_succeeded,
            pin_failed=pin_failed,
            forward_requested=forward_requested,
            forward_succeeded=forward_succeeded,
            forward_failed=forward_failed,
        )
