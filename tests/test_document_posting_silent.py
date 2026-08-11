from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.domain.content import PostDocument
from app.services.document_posting import DocumentPostingService


class _CaptureDocumentPostingService(DocumentPostingService):
    def __init__(self) -> None:
        self.classic_calls: list[tuple[int, dict]] = []
        self.rich_calls: list[dict] = []

    async def _resolve_media_assets(
        self,
        document: PostDocument,
        *,
        asset_channel_id: int | None,
    ) -> PostDocument:
        return document

    async def send_now(self, channel_id: int, payload: dict) -> list[int] | None:
        self.classic_calls.append((int(channel_id), dict(payload)))
        return [101]

    async def _send_with_retry(self, func, *args, **kwargs):
        self.rich_calls.append(dict(kwargs))
        return SimpleNamespace(message_id=202)


def _classic_document(*, document_silent: bool = False) -> PostDocument:
    return PostDocument(
        mode="classic",
        blocks=[
            {
                "id": "b1",
                "type": "text",
                "text": "Classic silent override",
            }
        ],
        telegram={"silent": document_silent},
    )


def _rich_document(*, document_silent: bool = False) -> PostDocument:
    return PostDocument(
        mode="rich",
        blocks=[
            {
                "id": "b1",
                "type": "paragraph",
                "content": "Rich silent override",
            }
        ],
        telegram={"silent": document_silent},
    )


def test_explicit_silent_override_is_forwarded_to_classic_payload() -> None:
    async def run() -> None:
        posting = _CaptureDocumentPostingService()

        assert await posting.send_document(
            -1001,
            _classic_document(),
            disable_notification=True,
        ) == [101]
        assert posting.classic_calls[-1][0] == -1001
        assert posting.classic_calls[-1][1]["silent"] is True

        assert await posting.send_document(
            -1001,
            _classic_document(document_silent=True),
            disable_notification=False,
        ) == [101]
        assert posting.classic_calls[-1][1]["silent"] is False

    asyncio.run(run())


def test_classic_none_override_preserves_historical_send_document_behavior() -> None:
    async def run() -> None:
        posting = _CaptureDocumentPostingService()
        assert await posting.send_document(
            -1002,
            _classic_document(document_silent=True),
        ) == [101]

        # Historical classic adapter did not copy document.telegram.silent into the
        # compatibility payload. `None` intentionally preserves that behavior; only
        # the new explicit runtime override changes classic delivery semantics.
        assert "silent" not in posting.classic_calls[-1][1]

    asyncio.run(run())


def test_explicit_silent_override_is_forwarded_to_rich_provider_call() -> None:
    async def run() -> None:
        posting = _CaptureDocumentPostingService()

        assert await posting.send_document(
            -1003,
            _rich_document(),
            disable_notification=True,
        ) == [202]
        assert posting.rich_calls[-1]["chat_id"] == -1003
        assert posting.rich_calls[-1]["disable_notification"] is True

        assert await posting.send_document(
            -1003,
            _rich_document(document_silent=True),
            disable_notification=False,
        ) == [202]
        assert posting.rich_calls[-1]["disable_notification"] is False

    asyncio.run(run())


def test_rich_none_override_preserves_renderer_document_default() -> None:
    async def run() -> None:
        posting = _CaptureDocumentPostingService()
        assert await posting.send_document(
            -1004,
            _rich_document(document_silent=True),
        ) == [202]
        assert posting.rich_calls[-1]["disable_notification"] is True

    asyncio.run(run())
