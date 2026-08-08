from __future__ import annotations

import asyncio

from app.bot.ai_editor_runtime import (
    ai_result_actions_kb,
    ai_result_summary,
    apply_generated_text,
    improve_action_name,
    request_prompt_key,
    run_editor_ai_request,
)
from app.bot.editor_preview import delete_tracked_preview_messages
from app.core.callbacks import CB


def test_improve_action_parser_preserves_compound_action() -> None:
    assert improve_action_name("ai_improve_style") == "style"
    assert improve_action_name("ai_improve_tone_menu") == "tone_menu"
    assert improve_action_name("ai_improve_tone_friendly") == "tone_friendly"


def test_generated_text_updates_text_or_media_payload() -> None:
    assert apply_generated_text({}, "hello") == {"type": "text", "text": "hello"}
    assert apply_generated_text({"type": "text", "text": "old"}, "new")["text"] == "new"
    media = apply_generated_text({"type": "photo", "file_id": "x"}, "caption")
    assert media["type"] == "photo"
    assert media["caption"] == "caption"
    assert media["file_id"] == "x"


def test_ai_result_keyboard_exposes_retry_reset_and_editor() -> None:
    callbacks = [
        str(button.callback_data)
        for row in ai_result_actions_kb().inline_keyboard
        for button in row
    ]
    assert callbacks == [
        CB.AI_RETRY_LAST.value,
        CB.AI_RESET_HISTORY.value,
        CB.AI_BACK_TO_PREVIEW.value,
    ]
    assert len(set(callbacks)) == 3


def test_ai_result_summary_uses_usage_breakdown_when_available() -> None:
    text = ai_result_summary(
        {
            "success": True,
            "text": "ok",
            "tokens_used": 40,
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "error": None,
        },
        label="Текст готов",
    )
    assert "40 токенов" in text
    assert "Запрос: 30 · ответ: 10" in text


def test_request_prompt_key_handles_missing_request() -> None:
    assert request_prompt_key(None) is None
    assert request_prompt_key({}) is None
    assert request_prompt_key({"prompt_key": "ai_text"}) == "ai_text"


def test_unknown_editor_ai_request_fails_without_network_call() -> None:
    async def run() -> None:
        result = await run_editor_ai_request(
            object(),
            session=object(),  # type: ignore[arg-type]
            request={"kind": "unknown"},
            channel_id=1,
            user_id=2,
            chat_id=3,
            seed=4,
        )
        assert result["success"] is False
        assert "Неизвестный" in str(result["error"])

    asyncio.run(run())


def test_preview_cleanup_deduplicates_tracked_message_ids() -> None:
    class _Bot:
        def __init__(self) -> None:
            self.deleted: list[int] = []

        async def delete_message(self, *, chat_id: int, message_id: int) -> None:
            self.deleted.append(message_id)

    async def run() -> None:
        bot = _Bot()
        await delete_tracked_preview_messages(
            bot,
            chat_id=1,
            state_data={
                "preview_msg_id": 10,
                "preview_text_id": 10,
                "preview_media_id": 11,
                "preview_album_ids": [11, 12, 13],
            },
        )
        assert set(bot.deleted) == {10, 11, 12, 13}
        assert len(bot.deleted) == 4

    asyncio.run(run())
