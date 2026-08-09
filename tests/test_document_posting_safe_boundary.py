from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from loguru import logger

from app.services.document_posting import DocumentPostingService


def _credential_like_error() -> tuple[str, str]:
    secret = ":".join(("123456", "FAKE_PROVIDER_TOKEN"))
    raw = (
        "provider rejected request "
        f"https://api.telegram.org/bot{secret}/sendMessage?access_token=private"
    )
    return secret, raw


def test_document_posting_redacts_dispatch_exception_text_from_logs() -> None:
    async def run() -> None:
        service = DocumentPostingService(SimpleNamespace(), lambda: None)
        secret, raw = _credential_like_error()

        async def failing_dispatch(channel_id: int, payload: dict):
            raise RuntimeError(raw)

        service._dispatch = failing_dispatch  # type: ignore[method-assign]
        messages: list[str] = []
        sink_id = logger.add(lambda message: messages.append(str(message)), format="{message}")
        try:
            result = await service.send_now(-100123456, {"type": "text", "text": "x"})
        finally:
            logger.remove(sink_id)

        assert result is None
        joined = "\n".join(messages)
        assert secret not in joined
        assert raw not in joined
        assert "error_type=RuntimeError" in joined

    asyncio.run(run())


def test_document_posting_forward_failure_falls_back_without_raw_error_log() -> None:
    async def run() -> None:
        secret, raw = _credential_like_error()

        async def failing_forward_message(*args, **kwargs):
            raise RuntimeError(raw)

        bot = SimpleNamespace(forward_message=failing_forward_message)
        service = DocumentPostingService(bot, lambda: None)

        async def fallback_dispatch(channel_id: int, payload: dict):
            assert channel_id == -100654321
            return [77]

        service._dispatch = fallback_dispatch  # type: ignore[method-assign]
        messages: list[str] = []
        sink_id = logger.add(lambda message: messages.append(str(message)), format="{message}")
        try:
            result = await service.send_now(
                -100654321,
                {
                    "type": "text",
                    "text": "fallback",
                    "forward_from_chat_id": -1001,
                    "forward_from_message_id": 9,
                },
            )
        finally:
            logger.remove(sink_id)

        assert result == [77]
        joined = "\n".join(messages)
        assert secret not in joined
        assert raw not in joined
        assert "forward fallback failed error_type=RuntimeError" in joined

    asyncio.run(run())


def test_document_posting_does_not_turn_cancellation_into_delivery_failure() -> None:
    async def run() -> None:
        service = DocumentPostingService(SimpleNamespace(), lambda: None)

        async def cancelled_dispatch(channel_id: int, payload: dict):
            raise asyncio.CancelledError

        service._dispatch = cancelled_dispatch  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await service.send_now(-100123456, {"type": "text", "text": "x"})

    asyncio.run(run())
