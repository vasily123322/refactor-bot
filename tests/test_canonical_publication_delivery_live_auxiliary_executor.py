from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger

from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecutor,
)
from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    CanonicalPublicationLiveAdminLogPlan,
    CanonicalPublicationLiveOwnerNoticePlan,
)


def _plan() -> CanonicalPublicationDeliveryLiveAuxiliaryPlan:
    return CanonicalPublicationDeliveryLiveAuxiliaryPlan(
        publication_id=7,
        admin_log=CanonicalPublicationLiveAdminLogPlan(
            publication_id=7,
            log_chat_id=-9000,
            source_telegram_chat_id=-100123,
            primary_message_id=77,
            result_link="https://t.me/c/123/77",
            author_tg_user_id=501,
            author_username="author_name",
            author_full_name="Author <Name>",
        ),
        owner_notice=CanonicalPublicationLiveOwnerNoticePlan(
            publication_id=7,
            owner_tg_user_id=502,
            owner_username="owner<unsafe>",
            channel_title="Channel <unsafe>",
            source_telegram_chat_id=-100123,
            result_link="https://t.me/c/123/77",
            delivered_count=1,
            timezone_code="UTC<script>",
            local_date_iso="2026-08-11",
            local_date_text="11.08.2026",
            local_time_text="12:00",
            callback_data="cp_open_pub:7:2026-08-11",
        ),
    )


class _Bot:
    def __init__(
        self,
        *,
        username: str | None = "public_channel",
        invite_link: str = "https://t.me/+SafeInvite_123",
        fail_chat_id: int | None = None,
        cancel_chat_id: int | None = None,
    ) -> None:
        self.username = username
        self.invite_link = invite_link
        self.fail_chat_id = fail_chat_id
        self.cancel_chat_id = cancel_chat_id
        self.calls: list[tuple[str, int, str | None, dict]] = []

    async def get_chat(self, chat_id: int):
        self.calls.append(("get_chat", int(chat_id), None, {}))
        return SimpleNamespace(username=self.username)

    async def create_chat_invite_link(self, **kwargs):
        self.calls.append(
            ("create_chat_invite_link", int(kwargs["chat_id"]), None, dict(kwargs))
        )
        return SimpleNamespace(invite_link=self.invite_link)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        safe_chat_id = int(chat_id)
        self.calls.append(("send_message", safe_chat_id, str(text), dict(kwargs)))
        if self.cancel_chat_id == safe_chat_id:
            raise asyncio.CancelledError
        if self.fail_chat_id == safe_chat_id:
            raise RuntimeError(
                "https://api.telegram.org/botSUPERSECRET/sendMessage failed"
            )
        return SimpleNamespace(message_id=999)


def _send_calls(bot: _Bot):
    return [call for call in bot.calls if call[0] == "send_message"]


def test_live_auxiliary_executor_preserves_admin_then_owner_order_and_safe_html() -> None:
    async def run() -> None:
        bot = _Bot()
        result = await CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot).execute(
            _plan()
        )

        assert result.admin_attempted == 1
        assert result.admin_sent == 1
        assert result.admin_failed == 0
        assert result.owner_attempted == 1
        assert result.owner_sent == 1
        assert result.owner_failed == 0
        assert result.invalid_plans == 0

        sends = _send_calls(bot)
        assert [call[1] for call in sends] == [-9000, 502]
        admin_text = sends[0][2] or ""
        owner_text = sends[1][2] or ""
        assert "https://t.me/author_name" in admin_text
        assert "https://t.me/c/123/77" in admin_text
        assert "https://t.me/public_channel" in admin_text
        assert "Channel &lt;unsafe&gt;" in owner_text
        assert "@owner&lt;unsafe&gt;" in owner_text
        assert "UTC&lt;script&gt;" in owner_text
        assert "<unsafe>" not in owner_text
        keyboard = sends[1][3]["reply_markup"]
        assert keyboard.inline_keyboard[0][0].callback_data == (
            "cp_open_pub:7:2026-08-11"
        )

    asyncio.run(run())


def test_admin_failure_is_redacted_and_owner_notice_still_runs() -> None:
    async def run() -> None:
        bot = _Bot(fail_chat_id=-9000)
        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}")
        try:
            result = await CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot).execute(
                _plan()
            )
        finally:
            logger.remove(sink_id)

        assert result.admin_sent == 0
        assert result.admin_failed == 1
        assert result.owner_sent == 1
        assert [call[1] for call in _send_calls(bot)] == [-9000, 502]
        rendered = "\n".join(messages)
        assert "SUPERSECRET" not in rendered
        assert "error_type=RuntimeError" in rendered

    asyncio.run(run())


def test_malformed_provider_username_and_invite_never_enter_html() -> None:
    async def run() -> None:
        bot = _Bot(
            username='bad"><script>alert(1)</script>',
            invite_link="https://evil.example/+secret",
        )
        result = await CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot).execute(
            _plan()
        )
        assert result.admin_sent == 1
        assert result.owner_sent == 1

        sends = _send_calls(bot)
        admin_text = sends[0][2] or ""
        owner_text = sends[1][2] or ""
        assert "evil.example" not in admin_text
        assert "<script>" not in admin_text
        assert "bad\"" not in admin_text
        assert " в канал/чат: -100123" in admin_text
        assert "<script>" not in owner_text
        assert "Канал: Channel &lt;unsafe&gt;" in owner_text

    asyncio.run(run())


def test_untrusted_manual_plan_links_and_negative_author_id_are_ignored() -> None:
    async def run() -> None:
        base = _plan()
        assert base.admin_log is not None and base.owner_notice is not None
        plan = replace(
            base,
            admin_log=replace(
                base.admin_log,
                result_link="https://evil.example/post",
                author_username=None,
                author_tg_user_id=-501,
                author_full_name="<script>author</script>",
            ),
            owner_notice=replace(
                base.owner_notice,
                result_link="https://evil.example/post",
            ),
        )
        bot = _Bot(
            username=None,
            invite_link="https://evil.example/+bad",
        )
        result = await CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot).execute(plan)
        assert result.admin_sent == 1
        assert result.owner_sent == 1

        sends = _send_calls(bot)
        admin_text = sends[0][2] or ""
        owner_text = sends[1][2] or ""
        assert "evil.example" not in admin_text
        assert "evil.example" not in owner_text
        assert "tg://user?id=-501" not in admin_text
        assert "<script>author</script>" not in admin_text
        # The invalid supplied post link is replaced by deterministic private evidence.
        assert "https://t.me/c/123/77" in admin_text

    asyncio.run(run())


def test_cancellation_during_admin_send_stops_chain_before_owner_notice() -> None:
    async def run() -> None:
        bot = _Bot(cancel_chat_id=-9000)
        with pytest.raises(asyncio.CancelledError):
            await CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot).execute(_plan())

        sends = _send_calls(bot)
        assert [call[1] for call in sends] == [-9000]

    asyncio.run(run())


def test_mismatched_nested_plan_identity_is_never_executed() -> None:
    async def run() -> None:
        plan = _plan()
        assert plan.admin_log is not None
        mismatched = CanonicalPublicationDeliveryLiveAuxiliaryPlan(
            publication_id=7,
            admin_log=CanonicalPublicationLiveAdminLogPlan(
                publication_id=8,
                log_chat_id=plan.admin_log.log_chat_id,
                source_telegram_chat_id=-100123,
                primary_message_id=77,
                result_link=None,
                author_tg_user_id=None,
                author_username=None,
                author_full_name=None,
            ),
            owner_notice=None,
        )
        bot = _Bot()
        result = await CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot).execute(
            mismatched
        )
        assert result.invalid_plans == 1
        assert result.admin_attempted == 0
        assert _send_calls(bot) == []

    asyncio.run(run())
