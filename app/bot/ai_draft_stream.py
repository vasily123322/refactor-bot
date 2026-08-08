from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.services.llm.openrouter_client import ChatResult, ChatStreamEvent
from app.services.telegram_drafts import TelegramDraftStreamer


def _missing_result() -> ChatResult:
    return {
        "success": False,
        "text": None,
        "tokens_used": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "error": "Поток генерации завершился без результата",
    }


async def render_ai_stream_to_draft(
    bot: Any,
    *,
    chat_id: int,
    seed: int,
    events: AsyncIterator[ChatStreamEvent],
) -> ChatResult:
    """Render a service stream into Telegram draft updates and return final result."""
    renderer = TelegramDraftStreamer(bot, chat_id=chat_id, seed=seed)
    await renderer.start()
    assembled = ""
    terminal: ChatResult | None = None
    try:
        async for event in events:
            kind = event.get("type")
            if kind == "delta":
                assembled += event.get("text") or ""
                await renderer.update(assembled)
            elif kind in {"done", "error"}:
                terminal = event.get("result")

        if terminal is None:
            terminal = _missing_result()
        if terminal.get("success"):
            await renderer.finish(terminal.get("text") or assembled)
        return terminal
    finally:
        await renderer.cleanup()
