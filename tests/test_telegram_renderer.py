from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.domain.content import PostDocument
from app.services.document_posting import DocumentPostingService
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


def test_renderer_keeps_classic_payload_compatible() -> None:
    document = PostDocument(
        blocks=[
            {
                "id": "b1",
                "type": "text",
                "text": "Classic",
                "entities": [{"type": "bold", "offset": 0, "length": 7}],
            }
        ],
        telegram={
            "buttons": [[{"text": "Open", "url": "https://example.com"}]],
            "silent": True,
            "protect_content": True,
        },
    )

    plan = TelegramRenderer().render(document)

    assert plan.kind == "classic"
    assert plan.classic_payload is not None
    assert plan.classic_payload["text"] == "Classic"
    assert plan.classic_payload["entities"][0]["type"] == "bold"
    assert plan.disable_notification is True
    assert plan.protect_content is True
    assert plan.reply_markup is not None
    assert plan.reply_markup.inline_keyboard[0][0].url == "https://example.com"


def test_renderer_builds_structured_rich_message() -> None:
    document = PostDocument(
        mode="rich",
        blocks=[
            {
                "id": "p1",
                "type": "paragraph",
                "content": [
                    {"text": "Bold", "marks": ["bold"]},
                    {"text": " and "},
                    {
                        "text": "link",
                        "marks": [{"type": "link", "url": "https://example.com"}],
                    },
                ],
            },
            {"id": "h1", "type": "heading", "size": 3, "content": "Heading"},
            {"id": "d1", "type": "divider"},
            {
                "id": "q1",
                "type": "quote",
                "content": "Block quote",
                "credit": "Editor",
            },
            {
                "id": "pq1",
                "type": "pull_quote",
                "content": "Pull quote",
            },
            {
                "id": "l1",
                "type": "list",
                "items": ["One", {"label": "A", "content": "Two"}],
            },
            {
                "id": "x1",
                "type": "details",
                "summary": "Details",
                "content": "Hidden body",
                "is_open": True,
            },
            {"id": "m1", "type": "math", "formula": "E=mc^2"},
            {"id": "a1", "type": "anchor", "name": "section-one"},
        ],
        telegram={"buttons": [[{"text": "Site", "url": "https://example.com"}]]},
    )

    plan = TelegramRenderer().render(document)

    assert plan.kind == "rich"
    assert plan.classic_payload is None
    assert plan.rich_message is not None
    assert plan.rich_message.blocks is not None
    assert [block.type for block in plan.rich_message.blocks] == [
        "paragraph",
        "heading",
        "divider",
        "block_quote",
        "pull_quote",
        "list",
        "details",
        "math",
        "anchor",
    ]
    paragraph = plan.rich_message.blocks[0]
    assert isinstance(paragraph.text, list)
    assert paragraph.text[0].type == "bold"
    assert paragraph.text[0].text == "Bold"
    assert paragraph.text[2].type == "url"
    assert paragraph.text[2].url == "https://example.com"
    assert plan.rich_message.blocks[1].size == 3
    assert plan.rich_message.blocks[5].items[1].label == "A"
    assert plan.rich_message.blocks[6].is_open is True
    assert plan.rich_message.blocks[7].expression == "E=mc^2"
    assert plan.reply_markup is not None


def test_renderer_validates_sizes_and_unsupported_media() -> None:
    with pytest.raises(TelegramRenderError, match="heading.size"):
        TelegramRenderer().render(
            PostDocument(
                mode="rich",
                blocks=[
                    {"id": "h", "type": "heading", "size": 7, "content": "Too big"}
                ],
            )
        )

    with pytest.raises(TelegramRenderError, match="file-attachment renderer"):
        TelegramRenderer().render(
            PostDocument(
                mode="rich",
                blocks=[{"id": "i", "type": "image", "asset_id": "asset-1"}],
            )
        )


@dataclass
class _Message:
    message_id: int


class _RichBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_rich_message(self, **kwargs):
        self.calls.append(kwargs)
        return _Message(message_id=700)


def test_document_posting_sends_rich_message_and_returns_id() -> None:
    async def run() -> None:
        bot = _RichBot()
        posting = DocumentPostingService(bot, object())  # type: ignore[arg-type]
        ids = await posting.send_document(
            12345,
            PostDocument(
                mode="rich",
                blocks=[{"id": "p", "type": "paragraph", "content": "Hello rich"}],
            ),
        )
        assert ids == [700]
        assert len(bot.calls) == 1
        assert bot.calls[0]["chat_id"] == 12345
        assert bot.calls[0]["rich_message"].blocks[0].text == "Hello rich"

    asyncio.run(run())


def test_document_posting_dispatches_serialized_rich_document_task_via_send_now() -> None:
    async def run() -> None:
        bot = _RichBot()
        posting = DocumentPostingService(bot, object())  # type: ignore[arg-type]
        document = PostDocument(
            mode="rich",
            blocks=[{"id": "p", "type": "paragraph", "content": "From scheduler"}],
        )
        ids = await posting.send_now(
            67890,
            {"type": "rich_document", "post_document": document.to_dict()},
        )
        assert ids == [700]
        assert bot.calls[0]["chat_id"] == 67890

    asyncio.run(run())
