from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from loguru import logger

from app.workers.publication_scheduler import (
    SAFE_AUXILIARY_ERROR,
    SAFE_DELETE_FORBIDDEN,
    SAFE_DELETE_NOT_FOUND,
    Scheduler,
    SchedulerAuxiliaryError,
)


class _FakeSession:
    async def commit(self) -> None:
        return None


class _Posting:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def send_now(self, *args, **kwargs):
        return [1]


class _SecretBot:
    def __init__(self, *, get_chat_error=None, delete_error=None) -> None:
        self.get_chat_error = get_chat_error
        self.delete_error = delete_error

    async def get_chat(self, *args, **kwargs):
        if self.get_chat_error is not None:
            raise self.get_chat_error
        return SimpleNamespace(username=None)

    async def delete_message(self, *args, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        return True

    async def send_message(self, *args, **kwargs):
        return True

    async def pin_chat_message(self, *args, **kwargs):
        return True

    async def forward_message(self, *args, **kwargs):
        return True


def test_auxiliary_provider_errors_are_redacted_before_legacy_logs() -> None:
    async def run() -> None:
        secret = "bot_token=123456:SUPER-SECRET https://api.example.test/?key=private"
        bot = _SecretBot(
            get_chat_error=RuntimeError(secret),
            delete_error=RuntimeError(
                f"Bad Request: message to delete not found {secret}"
            ),
        )
        scheduler = Scheduler(lambda: None, _Posting(bot))
        captured = io.StringIO()
        sink_id = logger.add(captured, level="DEBUG", format="{message}")
        try:
            post = SimpleNamespace(id=91, payload={})
            payload: dict = {}
            await scheduler._persist_result_fields(
                _FakeSession(),
                post,
                -10012345,
                payload,
                [7701],
            )
            assert payload["result_ids"] == [7701]
            assert payload["result_link"] == "https://t.me/c/12345/7701"

            await scheduler._del_later(
                scheduler.posting.bot,
                -10012345,
                [7701],
                0,
                91,
                False,
                None,
            )

            rendered = captured.getvalue()
            assert secret not in rendered
            assert "SUPER-SECRET" not in rendered
            assert "private" not in rendered
            assert "RuntimeError" in rendered
            assert SAFE_AUXILIARY_ERROR in rendered
            assert SAFE_DELETE_NOT_FOUND in rendered
        finally:
            logger.remove(sink_id)

    asyncio.run(run())


def test_delete_error_semantics_survive_redaction() -> None:
    async def safe_message(raw: str) -> str:
        scheduler = Scheduler(
            lambda: None,
            _Posting(_SecretBot(delete_error=RuntimeError(raw))),
        )
        with pytest.raises(SchedulerAuxiliaryError) as captured:
            await scheduler.posting.bot.delete_message(chat_id=-1001, message_id=1)
        assert captured.value.__cause__ is None
        return str(captured.value)

    assert asyncio.run(safe_message("Bad Request: MESSAGE_ID_INVALID token=secret")) == (
        SAFE_DELETE_NOT_FOUND
    )
    assert asyncio.run(safe_message("Bad Request: message can't be deleted token=secret")) == (
        SAFE_DELETE_FORBIDDEN
    )
    assert asyncio.run(safe_message("network exploded token=secret")) == SAFE_AUXILIARY_ERROR


def test_auxiliary_redaction_preserves_cancellation() -> None:
    async def run() -> None:
        scheduler = Scheduler(
            lambda: None,
            _Posting(_SecretBot(get_chat_error=asyncio.CancelledError())),
        )
        with pytest.raises(asyncio.CancelledError):
            await scheduler.posting.bot.get_chat(-1001)

    asyncio.run(run())
