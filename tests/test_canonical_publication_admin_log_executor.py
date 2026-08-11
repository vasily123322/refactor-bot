from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger

from app.services.canonical_publication_admin_log_executor import (
    CanonicalPublicationAdminLogExecutor,
)
from app.services.canonical_publication_admin_log_planner import (
    CanonicalPublicationAdminLogPlan,
)


class _CaptureBot:
    def __init__(
        self,
        *,
        username: str | None = "source_channel",
        invite_link: str | None = "https://t.me/+invite-safe",
    ) -> None:
        self.username = username
        self.invite_link = invite_link
        self.get_chat_calls: list[int] = []
        self.invite_calls: list[dict] = []
        self.send_calls: list[tuple[int, str, dict]] = []

    async def get_chat(self, chat_id: int):
        self.get_chat_calls.append(int(chat_id))
        return SimpleNamespace(username=self.username)

    async def create_chat_invite_link(self, **kwargs):
        self.invite_calls.append(dict(kwargs))
        return SimpleNamespace(invite_link=self.invite_link)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.send_calls.append((int(chat_id), str(text), dict(kwargs)))
        return SimpleNamespace(message_id=1)


class _LookupFailingBot(_CaptureBot):
    async def get_chat(self, chat_id: int):
        self.get_chat_calls.append(int(chat_id))
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/getChat failed")

    async def create_chat_invite_link(self, **kwargs):
        self.invite_calls.append(dict(kwargs))
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/invite failed")


class _SendFailingBot(_CaptureBot):
    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.send_calls.append((int(chat_id), str(text), dict(kwargs)))
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/sendMessage failed")


class _CancellingBot(_CaptureBot):
    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.send_calls.append((int(chat_id), str(text), dict(kwargs)))
        raise asyncio.CancelledError()


def _plan(*, result_link: str | None = "https://t.me/c/777/42"):
    return CanonicalPublicationAdminLogPlan(
        publication_id=101,
        log_chat_id=-999001,
        source_telegram_chat_id=-100777,
        primary_message_id=42,
        result_link=result_link,
        author_tg_user_id=555001,
        author_username="author_name",
        author_full_name="Author Name",
    )


def test_executor_prefers_canonical_result_link_and_public_channel_link() -> None:
    async def run() -> None:
        bot = _CaptureBot(username="source_channel")
        result = await CanonicalPublicationAdminLogExecutor(bot).execute(_plan())
        assert result.attempted == 1
        assert result.sent == 1
        assert result.failed == 0
        assert bot.get_chat_calls == [-100777]
        assert bot.invite_calls == []
        assert len(bot.send_calls) == 1
        chat_id, text, kwargs = bot.send_calls[0]
        assert chat_id == -999001
        assert kwargs == {"disable_web_page_preview": True}
        assert (
            'пользователь <a href="https://t.me/author_name">@author_name</a> '
            'отправил пост: <a href="https://t.me/c/777/42">ссылка</a> '
            'в канал/чат: <a href="https://t.me/source_channel">перейти</a>'
        ) == text

    asyncio.run(run())


def test_executor_reconstructs_private_post_link_and_uses_valid_invite() -> None:
    async def run() -> None:
        bot = _CaptureBot(username=None)
        result = await CanonicalPublicationAdminLogExecutor(bot).execute(
            _plan(result_link=None)
        )
        assert result.sent == 1
        assert len(bot.invite_calls) == 1
        assert bot.invite_calls[0] == {
            "chat_id": -100777,
            "name": "post-log",
            "creates_join_request": False,
        }
        text = bot.send_calls[0][1]
        assert '<a href="https://t.me/c/777/42">ссылка</a>' in text
        assert '<a href="https://t.me/+invite-safe">перейти</a>' in text

    asyncio.run(run())


def test_malformed_provider_username_and_invite_never_enter_html_links() -> None:
    async def run() -> None:
        bot = _CaptureBot(
            username='bad"><b>inject</b>',
            invite_link='https://evil.example/"><script>bad</script>',
        )
        plan = replace(
            _plan(result_link=None),
            author_username='author"><i>bad</i>',
            author_full_name='Name <Unsafe> & Co',
        )
        result = await CanonicalPublicationAdminLogExecutor(bot).execute(plan)
        assert result.sent == 1
        text = bot.send_calls[0][1]
        assert "<script>" not in text
        assert "https://evil.example" not in text
        assert "https://t.me/bad" not in text
        assert "https://t.me/author" not in text
        assert 'href="tg://user?id=555001"' in text
        assert "Name &lt;Unsafe&gt; &amp; Co" in text
        assert '<a href="https://t.me/c/777/42">ссылка</a>' in text
        assert text.endswith("в канал/чат: -100777")

    asyncio.run(run())


def test_metadata_lookup_failures_fall_back_without_logging_raw_provider_text() -> None:
    async def run() -> None:
        bot = _LookupFailingBot(username=None)
        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}")
        try:
            result = await CanonicalPublicationAdminLogExecutor(bot).execute(
                _plan(result_link=None)
            )
        finally:
            logger.remove(sink_id)
        assert result.sent == 1
        text = bot.send_calls[0][1]
        assert '<a href="https://t.me/c/777/42">ссылка</a>' in text
        assert text.endswith("в канал/чат: -100777")
        rendered = "\n".join(messages)
        assert "SUPERSECRET" not in rendered
        assert "error_type=RuntimeError" in rendered

    asyncio.run(run())


def test_send_failure_is_best_effort_and_raw_error_is_not_logged() -> None:
    async def run() -> None:
        bot = _SendFailingBot()
        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}")
        try:
            result = await CanonicalPublicationAdminLogExecutor(bot).execute(_plan())
        finally:
            logger.remove(sink_id)
        assert result.attempted == 1
        assert result.sent == 0
        assert result.failed == 1
        rendered = "\n".join(messages)
        assert "SUPERSECRET" not in rendered
        assert "error_type=RuntimeError" in rendered

    asyncio.run(run())


def test_admin_log_cancellation_propagates() -> None:
    async def run() -> None:
        bot = _CancellingBot()
        with pytest.raises(asyncio.CancelledError):
            await CanonicalPublicationAdminLogExecutor(bot).execute(_plan())
        assert len(bot.send_calls) == 1

    asyncio.run(run())
