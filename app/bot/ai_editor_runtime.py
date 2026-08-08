from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.ai_draft_stream import render_ai_stream_to_draft
from app.core.callbacks import CB
from app.services.ai_generation import AIGenerationService
from app.services.ai_streaming import InteractiveAIStreamingService
from app.services.llm.openrouter_client import ChatResult


_MEDIA_TYPES = {"photo", "video", "animation", "audio", "voice", "album"}


def ai_result_actions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Ещё вариант", callback_data=CB.AI_RETRY_LAST
                ),
                InlineKeyboardButton(
                    text="🧹 Новый диалог", callback_data=CB.AI_RESET_HISTORY
                ),
            ],
            [
                InlineKeyboardButton(
                    text="← В редактор", callback_data=CB.AI_BACK_TO_PREVIEW
                )
            ],
        ]
    )


def ai_result_summary(result: ChatResult, *, label: str = "Готово") -> str:
    total = int(result.get("tokens_used", 0) or 0)
    prompt = int(result.get("prompt_tokens", 0) or 0)
    completion = int(result.get("completion_tokens", 0) or 0)
    first = f"✅ {label}"
    if total > 0:
        first += f" · {total} токенов"
    if prompt > 0 or completion > 0:
        return f"{first}\nЗапрос: {prompt} · ответ: {completion}"
    return first


def apply_generated_text(payload: dict[str, Any] | None, text: str) -> dict[str, Any]:
    updated = dict(payload or {})
    if not updated:
        return {"type": "text", "text": text}
    if updated.get("type") in _MEDIA_TYPES:
        updated["caption"] = text
    else:
        updated["type"] = "text"
        updated["text"] = text
    return updated


def request_prompt_key(request: dict[str, Any] | None) -> str | None:
    if not request:
        return None
    value = request.get("prompt_key")
    return str(value) if value else None


async def run_editor_ai_request(
    bot: Any,
    *,
    session: AsyncSession,
    request: dict[str, Any],
    channel_id: int,
    user_id: int,
    chat_id: int,
    seed: int,
) -> ChatResult:
    generation = AIGenerationService(session)
    streaming = InteractiveAIStreamingService(generation)
    kind = str(request.get("kind") or "")
    prompt_key = request_prompt_key(request)

    if kind == "topic":
        events = streaming.stream_pipeline(
            channel_id=channel_id,
            mode="from_scratch",
            topic=str(request.get("topic") or ""),
            extra=dict(request.get("extra") or {}),
            user_id=user_id,
            prompt_key=prompt_key,
        )
    elif kind == "improve":
        events = streaming.stream_pipeline(
            channel_id=channel_id,
            mode="improve",
            original_text=str(request.get("original_text") or ""),
            instruction=str(request.get("instruction") or "улучши текст"),
            user_id=user_id,
            prompt_key=prompt_key,
        )
    elif kind == "link":
        events = streaming.stream_from_link(
            channel_id=channel_id,
            url=str(request.get("url") or ""),
            mode=str(request.get("mode") or "summary"),
            user_id=user_id,
            prompt_key=prompt_key,
            force_custom=bool(request.get("force_custom", False)),
        )
    else:
        return {
            "success": False,
            "text": None,
            "tokens_used": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "error": "Неизвестный тип AI-запроса",
        }

    return await render_ai_stream_to_draft(
        bot,
        chat_id=chat_id,
        seed=seed,
        events=events,
    )
