from __future__ import annotations

import asyncio

from aiogram import Bot

from app.bot.bot_instance import RedactingBot
from app.core.redaction import REDACTED, redact_log_record, redact_secret_text


TELEGRAM_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghiJKLMN"
OPENAI_STYLE_KEY = "sk-test_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
GOOGLE_KEY = "AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"


def test_redact_secret_text_removes_common_credentials() -> None:
    raw = (
        f"telegram={TELEGRAM_TOKEN} "
        f"api_key={OPENAI_STYLE_KEY} "
        f"google={GOOGLE_KEY} "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345 "
        "password=super-secret-value"
    )

    redacted = redact_secret_text(raw)

    assert TELEGRAM_TOKEN not in redacted
    assert OPENAI_STYLE_KEY not in redacted
    assert GOOGLE_KEY not in redacted
    assert "abcdefghijklmnopqrstuvwxyz012345" not in redacted
    assert "super-secret-value" not in redacted
    assert redacted.count(REDACTED) >= 5


def test_redact_secret_text_preserves_normal_callback_data() -> None:
    value = "source_draft_open_12_3 settings_post_42 https://example.com/post"
    assert redact_secret_text(value) == value


def test_redact_secret_text_hides_url_password() -> None:
    value = "proxy=https://worker:very-secret-password@example.com:8443"
    redacted = redact_secret_text(value)

    assert "very-secret-password" not in redacted
    assert "worker" in redacted
    assert "example.com" in redacted


def test_log_record_redacts_message_and_nested_extra() -> None:
    record = {
        "message": f"external bot token={TELEGRAM_TOKEN}",
        "extra": {
            "credential": OPENAI_STYLE_KEY,
            "nested": {"auth": f"Authorization: Bearer {'x' * 32}"},
        },
    }

    assert redact_log_record(record) is True
    rendered = repr(record)
    assert TELEGRAM_TOKEN not in rendered
    assert OPENAI_STYLE_KEY not in rendered
    assert "x" * 32 not in rendered


def test_main_bot_redacts_admin_message_before_send(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_send_message(self, chat_id, text, *args, **kwargs):
        captured["text"] = text
        return object()

    monkeypatch.setattr(Bot, "send_message", fake_send_message)
    client = RedactingBot(token=TELEGRAM_TOKEN)

    async def run() -> None:
        try:
            await client.send_message(
                1,
                f"пользователь добавил бот с токеном: <code>{TELEGRAM_TOKEN}</code>",
            )
        finally:
            await client.session.close()

    asyncio.run(run())

    assert TELEGRAM_TOKEN not in captured["text"]
    assert REDACTED in captured["text"]
