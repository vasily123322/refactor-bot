from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger

from app.services.canonical_publication_owner_notice_executor import (
    CanonicalPublicationOwnerNoticeExecutor,
)
from app.services.canonical_publication_owner_notice_planner import (
    CanonicalPublicationOwnerNoticePlan,
)


class _CaptureBot:
    def __init__(self, *, username: str | None = "source_channel") -> None:
        self.username = username
        self.get_chat_calls: list[int] = []
        self.send_calls: list[dict] = []

    async def get_chat(self, chat_id: int):
        self.get_chat_calls.append(int(chat_id))
        return SimpleNamespace(username=self.username)

    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        return SimpleNamespace(message_id=1)


class _LookupFailingBot(_CaptureBot):
    async def get_chat(self, chat_id: int):
        self.get_chat_calls.append(int(chat_id))
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/getChat failed")


class _SendFailingBot(_CaptureBot):
    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/sendMessage failed")


class _CancellingBot(_CaptureBot):
    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        raise asyncio.CancelledError()


def _plan() -> CanonicalPublicationOwnerNoticePlan:
    return CanonicalPublicationOwnerNoticePlan(
        publication_id=91,
        owner_tg_user_id=555001,
        owner_username="owner_name",
        channel_title="A <Channel> & Co",
        source_telegram_chat_id=-100777,
        result_link="https://t.me/c/777/42",
        delivered_count=2,
        timezone_code="Europe/London",
        local_date_iso="2026-08-11",
        local_date_text="11.08.2026",
        local_time_text="13:30",
        callback_data="cp_open_pub:91:2026-08-11",
    )


def test_notice_uses_canonical_callback_and_legacy_compatible_text() -> None:
    async def run() -> None:
        bot = _CaptureBot()
        result = await CanonicalPublicationOwnerNoticeExecutor(bot).execute(_plan())
        assert result.attempted == 1
        assert result.sent == 1
        assert result.failed == 0
        assert bot.get_chat_calls == [-100777]
        assert len(bot.send_calls) == 1
        call = bot.send_calls[0]
        assert call["chat_id"] == 555001
        assert call["parse_mode"] == "HTML"
        assert call["disable_web_page_preview"] is True
        text = call["text"]
        assert "✅ Пост успешно опубликован" in text
        assert "🔗 Ссылка на пост https://t.me/c/777/42" in text
        assert "📅 11.08.2026 • 🕔 13:30 (Europe/London)" in text
        assert "👀 Доставлено: 2/1" in text
        assert "Переслано: 0" in text
        assert (
            'Канал: <a href="https://t.me/source_channel">A &lt;Channel&gt; &amp; Co</a> '
            "| Автор: @owner_name"
        ) in text
        button = call["reply_markup"].inline_keyboard[0][0]
        assert button.text == "Редактировать"
        assert button.callback_data == "cp_open_pub:91:2026-08-11"

    asyncio.run(run())


def test_provider_username_and_timezone_are_safely_rendered_in_html() -> None:
    async def run() -> None:
        bot = _CaptureBot(username='bad"><b>inject</b>')
        plan = replace(
            _plan(),
            owner_username="owner_<unsafe>",
            timezone_code='UTC</b><script>bad</script>',
        )
        result = await CanonicalPublicationOwnerNoticeExecutor(bot).execute(plan)
        assert result.sent == 1
        text = bot.send_calls[0]["text"]
        assert "https://t.me/bad" not in text
        assert "<script>" not in text
        assert "UTC&lt;/b&gt;&lt;script&gt;bad&lt;/script&gt;" in text
        assert "@owner_&lt;unsafe&gt;" in text
        assert "Канал: A &lt;Channel&gt; &amp; Co" in text

    asyncio.run(run())


def test_channel_lookup_failure_is_best_effort_and_notice_still_sends() -> None:
    async def run() -> None:
        bot = _LookupFailingBot()
        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}")
        try:
            result = await CanonicalPublicationOwnerNoticeExecutor(bot).execute(_plan())
        finally:
            logger.remove(sink_id)
        assert result.sent == 1
        assert len(bot.send_calls) == 1
        assert "Канал: A &lt;Channel&gt; &amp; Co | Автор: @owner_name" in bot.send_calls[0][
            "text"
        ]
        rendered = "\n".join(messages)
        assert "SUPERSECRET" not in rendered
        assert "error_type=RuntimeError" in rendered

    asyncio.run(run())


def test_notice_send_failure_is_counted_without_logging_raw_provider_text() -> None:
    async def run() -> None:
        bot = _SendFailingBot()
        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}")
        try:
            result = await CanonicalPublicationOwnerNoticeExecutor(bot).execute(_plan())
        finally:
            logger.remove(sink_id)
        assert result.attempted == 1
        assert result.sent == 0
        assert result.failed == 1
        rendered = "\n".join(messages)
        assert "SUPERSECRET" not in rendered
        assert "error_type=RuntimeError" in rendered

    asyncio.run(run())


def test_notice_cancellation_propagates() -> None:
    async def run() -> None:
        bot = _CancellingBot()
        with pytest.raises(asyncio.CancelledError):
            await CanonicalPublicationOwnerNoticeExecutor(bot).execute(_plan())
        assert len(bot.send_calls) == 1

    asyncio.run(run())
