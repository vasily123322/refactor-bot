from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.api.studio.schemas import TelegramPreviewRequest
from app.domain.content import PostDocument
from app.services.telegram_preview import TelegramPreviewError, TelegramPreviewService


class _FakeBot:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


class _FakePosting:
    next_result: list[int] | None = [501, 502]
    calls: list[tuple[int, dict]] = []

    def __init__(self, bot, session_factory) -> None:
        self.bot = bot
        self.session_factory = session_factory

    async def send_now(self, chat_id: int, payload: dict) -> list[int] | None:
        type(self).calls.append((chat_id, payload))
        return type(self).next_result


def _document() -> PostDocument:
    return PostDocument(
        blocks=[{"id": "b1", "type": "text", "text": "Exact preview"}],
        telegram={"buttons": [[{"text": "Open", "url": "https://example.com"}]]},
    )


def test_exact_preview_uses_authenticated_chat_and_production_payload() -> None:
    async def run() -> None:
        _FakePosting.calls = []
        _FakePosting.next_result = [501, 502]
        bot = _FakeBot()
        service = TelegramPreviewService(
            bot,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            posting_factory=_FakePosting,  # type: ignore[arg-type]
        )
        ids = await service.send(
            tg_user_id=777,
            document=_document(),
            replace_message_ids=[400, 501, 401],
        )

        assert ids == [501, 502]
        assert _FakePosting.calls == [
            (
                777,
                {
                    "type": "text",
                    "text": "Exact preview",
                    "buttons": [[{"text": "Open", "url": "https://example.com"}]],
                },
            )
        ]
        # A newly returned id is never deleted even if the client sent it as stale.
        assert bot.deleted == [(777, 400), (777, 401)]

    asyncio.run(run())


def test_exact_preview_keeps_previous_preview_when_delivery_fails() -> None:
    async def run() -> None:
        _FakePosting.calls = []
        _FakePosting.next_result = None
        bot = _FakeBot()
        service = TelegramPreviewService(
            bot,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            posting_factory=_FakePosting,  # type: ignore[arg-type]
        )

        with pytest.raises(TelegramPreviewError, match="could not be delivered"):
            await service.send(
                tg_user_id=888,
                document=_document(),
                replace_message_ids=[10, 11],
            )

        assert bot.deleted == []

    asyncio.run(run())


def test_exact_preview_rejects_rich_content_until_shared_renderer_exists() -> None:
    async def run() -> None:
        bot = _FakeBot()
        service = TelegramPreviewService(
            bot,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            posting_factory=_FakePosting,  # type: ignore[arg-type]
        )
        document = PostDocument(
            mode="rich",
            blocks=[{"id": "p1", "type": "paragraph", "content": "Rich"}],
        )
        with pytest.raises(TelegramPreviewError, match="new Telegram renderer"):
            await service.send(tg_user_id=999, document=document)

    asyncio.run(run())


def test_preview_request_cannot_select_arbitrary_telegram_chat() -> None:
    document = _document().to_dict()
    with pytest.raises(ValidationError):
        TelegramPreviewRequest.model_validate(
            {
                "document": document,
                "replace_message_ids": [],
                "chat_id": -100123,
            }
        )
