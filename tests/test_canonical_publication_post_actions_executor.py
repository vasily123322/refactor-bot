from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from app.services.canonical_publication_post_actions_executor import (
    CanonicalPublicationPostDeliveryActionExecutor,
)
from app.services.canonical_publication_post_actions_planner import (
    CanonicalPublicationForwardTarget,
    CanonicalPublicationPostDeliveryActionPlan,
)


class _CaptureBot:
    def __init__(self) -> None:
        self.pin_calls: list[dict] = []
        self.forward_calls: list[dict] = []

    async def pin_chat_message(self, **kwargs):
        self.pin_calls.append(dict(kwargs))
        return object()

    async def forward_message(self, **kwargs):
        self.forward_calls.append(dict(kwargs))
        return object()


class _PartiallyFailingBot(_CaptureBot):
    async def pin_chat_message(self, **kwargs):
        self.pin_calls.append(dict(kwargs))
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/pin failed")

    async def forward_message(self, **kwargs):
        self.forward_calls.append(dict(kwargs))
        if int(kwargs["message_id"]) == 2002:
            raise ValueError("provider SUPERSECRET forward failed")
        return object()


class _CancellingBot(_CaptureBot):
    async def pin_chat_message(self, **kwargs):
        self.pin_calls.append(dict(kwargs))
        raise asyncio.CancelledError()


def _plan() -> CanonicalPublicationPostDeliveryActionPlan:
    return CanonicalPublicationPostDeliveryActionPlan(
        publication_id=77,
        source_channel_id=10,
        source_telegram_chat_id=-10010,
        message_ids=(2001, 2002),
        pin_last_message=True,
        forward_targets=(
            CanonicalPublicationForwardTarget(
                channel_id=20,
                telegram_chat_id=-10020,
            ),
            CanonicalPublicationForwardTarget(
                channel_id=30,
                telegram_chat_id=-10030,
            ),
        ),
        forward_silent=True,
    )


def test_executor_uses_last_id_for_pin_and_preserves_forward_order_and_silent() -> None:
    async def run() -> None:
        bot = _CaptureBot()
        result = await CanonicalPublicationPostDeliveryActionExecutor(bot).execute(_plan())

        assert result.publication_id == 77
        assert result.pin_requested == 1
        assert result.pin_succeeded == 1
        assert result.pin_failed == 0
        assert result.forward_requested == 4
        assert result.forward_succeeded == 4
        assert result.forward_failed == 0
        assert bot.pin_calls == [
            {"chat_id": -10010, "message_id": 2002},
        ]
        assert bot.forward_calls == [
            {
                "chat_id": -10020,
                "from_chat_id": -10010,
                "message_id": 2001,
                "disable_notification": True,
            },
            {
                "chat_id": -10020,
                "from_chat_id": -10010,
                "message_id": 2002,
                "disable_notification": True,
            },
            {
                "chat_id": -10030,
                "from_chat_id": -10010,
                "message_id": 2001,
                "disable_notification": True,
            },
            {
                "chat_id": -10030,
                "from_chat_id": -10010,
                "message_id": 2002,
                "disable_notification": True,
            },
        ]

    asyncio.run(run())


def test_provider_failures_are_best_effort_counted_and_raw_text_is_not_logged() -> None:
    async def run() -> None:
        bot = _PartiallyFailingBot()
        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}")
        try:
            result = await CanonicalPublicationPostDeliveryActionExecutor(bot).execute(
                _plan()
            )
        finally:
            logger.remove(sink_id)

        assert result.pin_requested == 1
        assert result.pin_succeeded == 0
        assert result.pin_failed == 1
        assert result.forward_requested == 4
        assert result.forward_succeeded == 2
        assert result.forward_failed == 2
        rendered = "\n".join(messages)
        assert "SUPERSECRET" not in rendered
        assert "error_type=RuntimeError" in rendered
        assert "error_type=ValueError" in rendered

    asyncio.run(run())


def test_empty_plan_performs_no_provider_calls() -> None:
    async def run() -> None:
        bot = _CaptureBot()
        empty = CanonicalPublicationPostDeliveryActionPlan(
            publication_id=88,
            source_channel_id=11,
            source_telegram_chat_id=-10011,
            message_ids=(3001,),
            pin_last_message=False,
            forward_targets=(),
            forward_silent=False,
        )
        result = await CanonicalPublicationPostDeliveryActionExecutor(bot).execute(empty)
        assert result.pin_requested == 0
        assert result.forward_requested == 0
        assert bot.pin_calls == []
        assert bot.forward_calls == []

    asyncio.run(run())


def test_cancellation_is_not_swallowed_or_reclassified_as_best_effort_failure() -> None:
    async def run() -> None:
        bot = _CancellingBot()
        with pytest.raises(asyncio.CancelledError):
            await CanonicalPublicationPostDeliveryActionExecutor(bot).execute(_plan())
        assert len(bot.pin_calls) == 1
        assert bot.forward_calls == []

    asyncio.run(run())
