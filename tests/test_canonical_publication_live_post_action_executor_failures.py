from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.canonical_publication_delivery_action_ledger import (
    CanonicalPublicationDeliveryActionReservation,
)
from app.services.canonical_publication_delivery_live_post_action_executor import (
    CanonicalPublicationDeliveryLivePostActionExecutor,
)
from app.services.canonical_publication_delivery_live_post_actions import (
    CanonicalPublicationDeliveryLivePostActionPlan,
    CanonicalPublicationLivePostAction,
)


_ACTION = CanonicalPublicationLivePostAction(
    publication_id=501,
    action_key="forward:77:2701",
    action_type="forward",
    intent_fingerprint="a" * 64,
    source_telegram_chat_id=-100501,
    source_message_id=2701,
    target_channel_id=77,
    target_telegram_chat_id=-100577,
    disable_notification=True,
)
_RESERVATION = CanonicalPublicationDeliveryActionReservation(
    publication_id=501,
    action_key=_ACTION.action_key,
    action_type="forward",
    intent_fingerprint=_ACTION.intent_fingerprint,
    delivery_lease_token="lease-token-501",
)
_CONTEXT = SimpleNamespace(publication_id=501)


class _FailureBot:
    def __init__(self) -> None:
        self.calls = 0

    async def forward_message(self, **kwargs) -> None:
        self.calls += 1
        raise RuntimeError("provider-secret-must-not-be-logged-or-retried")


class _CancelBot:
    def __init__(self) -> None:
        self.calls = 0

    async def forward_message(self, **kwargs) -> None:
        self.calls += 1
        raise asyncio.CancelledError


class _StateMachineExecutor(CanonicalPublicationDeliveryLivePostActionExecutor):
    def __init__(self, *, bot) -> None:
        super().__init__(bot=bot, session_factory=object())  # type: ignore[arg-type]
        self.has_reservation = False
        self.marks: list[str] = []

    async def _plan(self, context):
        return CanonicalPublicationDeliveryLivePostActionPlan(
            publication_id=501,
            actions=(_ACTION,),
        )

    async def _reserve(self, context, action):
        if self.has_reservation:
            return SimpleNamespace(
                outcome="already_reserved",
                reservation=None,
                existing_state=self.marks[-1] if self.marks else "reserved",
            )
        self.has_reservation = True
        return SimpleNamespace(
            outcome="reserved",
            reservation=_RESERVATION,
            existing_state="reserved",
        )

    async def _mark(self, reservation, state: str) -> bool:
        self.marks.append(state)
        return True


def test_provider_failure_marks_unknown_and_replay_never_calls_provider_again() -> None:
    async def run() -> None:
        bot = _FailureBot()
        executor = _StateMachineExecutor(bot=bot)

        first = await executor.execute(_CONTEXT)  # type: ignore[arg-type]
        assert first.reserved == 1
        assert first.unknown == 1
        assert first.succeeded == 0
        assert bot.calls == 1
        assert executor.marks == ["unknown"]

        second = await executor.execute(_CONTEXT)  # type: ignore[arg-type]
        assert second.skipped_reserved == 1
        assert bot.calls == 1
        assert executor.marks == ["unknown"]

    asyncio.run(run())


def test_provider_cancellation_marks_unknown_when_possible_and_propagates() -> None:
    async def run() -> None:
        bot = _CancelBot()
        executor = _StateMachineExecutor(bot=bot)

        try:
            await executor.execute(_CONTEXT)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("post-action cancellation must propagate")

        assert bot.calls == 1
        assert executor.has_reservation is True
        assert executor.marks == ["unknown"]

    asyncio.run(run())
