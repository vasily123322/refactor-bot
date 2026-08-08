from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.bot.ai_draft_stream import render_ai_stream_to_draft
from app.services.ai_streaming import _float_setting
from app.services.llm.openrouter_client import OpenRouterClient
from app.services.telegram_drafts import TelegramDraftStreamer, _clip_draft_text


def test_openrouter_sse_parser_ignores_comments_and_done() -> None:
    assert OpenRouterClient._parse_sse_line(": OPENROUTER PROCESSING") is None
    assert OpenRouterClient._parse_sse_line("data: [DONE]") is None
    assert OpenRouterClient._parse_sse_line("") is None
    assert OpenRouterClient._parse_sse_line("event: ping") is None


def test_openrouter_sse_parser_reads_json_chunk() -> None:
    parsed = OpenRouterClient._parse_sse_line(
        'data: {"choices":[{"delta":{"content":"hello"}}],"usage":null}'
    )
    assert parsed is not None
    assert parsed["choices"][0]["delta"]["content"] == "hello"


def test_openrouter_sse_parser_ignores_malformed_json() -> None:
    assert OpenRouterClient._parse_sse_line("data: {not-json") is None


def test_draft_clip_respects_telegram_limit() -> None:
    value = _clip_draft_text("x" * 5000)
    assert len(value) == 4096
    assert value.endswith("…")


def test_zero_sampling_values_are_preserved() -> None:
    assert _float_setting({"temperature": 0}, "temperature", 0.7) == 0.0
    assert _float_setting({"top_p": 0}, "top_p", 1.0) == 0.0
    assert _float_setting({}, "temperature", 0.7) == 0.7


class _FallbackMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


class _DraftBot:
    def __init__(self, *, fail_draft: bool = False) -> None:
        self.fail_draft = fail_draft
        self.drafts: list[tuple[int, int, str]] = []
        self.fallbacks: list[_FallbackMessage] = []

    async def send_message_draft(self, *, chat_id: int, draft_id: int, text: str):
        if self.fail_draft:
            raise RuntimeError("draft unsupported")
        self.drafts.append((chat_id, draft_id, text))
        return True

    async def send_message(self, *, chat_id: int, text: str):
        msg = _FallbackMessage()
        self.fallbacks.append(msg)
        return msg


def test_telegram_draft_streamer_uses_native_drafts() -> None:
    async def run() -> None:
        bot = _DraftBot()
        renderer = TelegramDraftStreamer(bot, chat_id=10, seed=20, min_new_chars=1)
        await renderer.start()
        await renderer.update("hello", force=True)
        await renderer.finish("hello world")
        await renderer.cleanup()
        assert bot.drafts[0][2] == ""
        assert bot.drafts[-1][2] == "hello world"
        assert bot.fallbacks == []
        assert renderer.draft_id != 0

    asyncio.run(run())


def test_telegram_draft_streamer_falls_back_to_editable_message() -> None:
    async def run() -> None:
        bot = _DraftBot(fail_draft=True)
        renderer = TelegramDraftStreamer(bot, chat_id=10, seed=20, min_new_chars=1)
        await renderer.start()
        await renderer.update("partial", force=True)
        assert len(bot.fallbacks) == 1
        assert bot.fallbacks[0].edits[-1] == "partial"
        await renderer.cleanup()
        assert bot.fallbacks[0].deleted is True

    asyncio.run(run())


def test_stream_bridge_returns_terminal_result_and_renders_text() -> None:
    async def events():
        yield {"type": "delta", "text": "hello "}
        yield {"type": "delta", "text": "world"}
        yield {
            "type": "done",
            "result": {
                "success": True,
                "text": "hello world",
                "tokens_used": 5,
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "error": None,
            },
        }

    async def run() -> None:
        bot = _DraftBot()
        result = await render_ai_stream_to_draft(
            bot,
            chat_id=100,
            seed=7,
            events=events(),
        )
        assert result["success"] is True
        assert result["text"] == "hello world"
        assert bot.drafts[-1][2] == "hello world"

    asyncio.run(run())
