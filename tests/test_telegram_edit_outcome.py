from __future__ import annotations

import asyncio

import pytest

import app.services.telegram_edit_outcome as edit_module
from app.services.telegram_edit_outcome import (
    TelegramEditFailed,
    TelegramEditOutcomeService,
)


class FakeBadRequest(Exception):
    pass


class FakeProvider:
    def __init__(self, *, text_plan=None, media_plan=None) -> None:
        self.text_plan = dict(text_plan or {})
        self.media_plan = dict(media_plan or {})
        self.text_calls: list[int] = []
        self.media_calls: list[int] = []

    async def edit_message_text(self, **kwargs):
        message_id = int(kwargs["message_id"])
        self.text_calls.append(message_id)
        outcome = self.text_plan.get(message_id)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def edit_message_media(self, **kwargs):
        message_id = int(kwargs["message_id"])
        self.media_calls.append(message_id)
        outcome = self.media_plan.get(message_id)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_edit_text_falls_back_only_after_bad_request(monkeypatch) -> None:
    monkeypatch.setattr(edit_module, "TelegramBadRequest", FakeBadRequest)

    async def run() -> None:
        provider = FakeProvider(
            text_plan={
                10: FakeBadRequest("provider secret primary"),
                12: object(),
            }
        )
        outcome = await TelegramEditOutcomeService(provider).edit_text(
            chat_id=-1001,
            primary_message_id=10,
            candidate_message_ids=[11, 12, 10],
            text="Edited",
        )
        assert outcome.message_id == 12
        assert outcome.attempted_message_ids == (10, 12)
        assert provider.text_calls == [10, 12]

    asyncio.run(run())


def test_edit_text_full_failure_is_safe_and_truthful(monkeypatch) -> None:
    monkeypatch.setattr(edit_module, "TelegramBadRequest", FakeBadRequest)

    async def run() -> None:
        provider = FakeProvider(
            text_plan={
                10: FakeBadRequest("https://provider.invalid/secret-primary"),
                12: FakeBadRequest("credential-like-secret"),
                11: FakeBadRequest("raw provider detail"),
            }
        )
        with pytest.raises(TelegramEditFailed) as captured:
            await TelegramEditOutcomeService(provider).edit_text(
                chat_id=-1001,
                primary_message_id=10,
                candidate_message_ids=[11, 12, 10],
                text="Edited",
            )
        assert str(captured.value) == "telegram edit failed"
        assert captured.value.provider_error_type == "FakeBadRequest"
        assert "secret" not in str(captured.value).lower()
        assert captured.value.__cause__ is None
        assert provider.text_calls == [10, 12, 11]

    asyncio.run(run())


def test_unexpected_edit_error_does_not_probe_other_messages(monkeypatch) -> None:
    monkeypatch.setattr(edit_module, "TelegramBadRequest", FakeBadRequest)

    async def run() -> None:
        provider = FakeProvider(
            text_plan={10: RuntimeError("network credential-like detail")}
        )
        with pytest.raises(TelegramEditFailed) as captured:
            await TelegramEditOutcomeService(provider).edit_text(
                chat_id=-1001,
                primary_message_id=10,
                candidate_message_ids=[11, 12],
                text="Edited",
            )
        assert str(captured.value) == "telegram edit failed"
        assert captured.value.provider_error_type == "RuntimeError"
        assert captured.value.__cause__ is None
        assert provider.text_calls == [10]

    asyncio.run(run())


def test_edit_media_returns_exact_confirmed_fallback_message(monkeypatch) -> None:
    monkeypatch.setattr(edit_module, "TelegramBadRequest", FakeBadRequest)

    async def run() -> None:
        provider = FakeProvider(
            media_plan={
                20: FakeBadRequest("wrong legacy primary"),
                22: object(),
            }
        )
        outcome = await TelegramEditOutcomeService(provider).edit_media(
            chat_id=-1002,
            primary_message_id=20,
            candidate_message_ids=[21, 22, 20],
            media=object(),
        )
        assert outcome.message_id == 22
        assert outcome.attempted_message_ids == (20, 22)
        assert provider.media_calls == [20, 22]

    asyncio.run(run())


def test_invalid_message_identity_fails_without_provider_call(monkeypatch) -> None:
    monkeypatch.setattr(edit_module, "TelegramBadRequest", FakeBadRequest)

    async def run() -> None:
        provider = FakeProvider()
        with pytest.raises(TelegramEditFailed) as captured:
            await TelegramEditOutcomeService(provider).edit_text(
                chat_id=-1001,
                primary_message_id=0,
                candidate_message_ids=[-1],
                text="Edited",
            )
        assert captured.value.provider_error_type == "InvalidMessageId"
        assert provider.text_calls == []

    asyncio.run(run())
