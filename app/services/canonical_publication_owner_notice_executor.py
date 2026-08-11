from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from app.services.canonical_publication_owner_notice_planner import (
    CanonicalPublicationOwnerNoticePlan,
)


_TELEGRAM_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class CanonicalOwnerNoticeBot(Protocol):
    async def get_chat(self, chat_id: int): ...

    async def send_message(self, chat_id: int, text: str, **kwargs): ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationOwnerNoticeExecution:
    publication_id: int
    attempted: int
    sent: int
    failed: int


class CanonicalPublicationOwnerNoticeExecutor:
    """Send one proven canonical owner notice with best-effort legacy semantics.

    This is a one-shot auxiliary primitive. Sending the same notice twice is visible to
    the owner, so a future coordinator must not use this executor as a retryable polling
    worker without first adding durable completion identity.
    """

    def __init__(self, bot: CanonicalOwnerNoticeBot) -> None:
        self.bot = bot

    async def execute(
        self,
        plan: CanonicalPublicationOwnerNoticePlan,
    ) -> CanonicalPublicationOwnerNoticeExecution:
        channel_link: str | None = None
        try:
            chat = await self.bot.get_chat(int(plan.source_telegram_chat_id))
            username = getattr(chat, "username", None)
            if username:
                username_text = str(username).strip().lstrip("@")
                if _TELEGRAM_USERNAME.fullmatch(username_text):
                    channel_link = f"https://t.me/{username_text}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Canonical owner notice channel lookup failed publication_id={} "
                "error_type={}",
                int(plan.publication_id),
                type(exc).__name__,
            )

        author = (
            html.escape(f"@{plan.owner_username}")
            if plan.owner_username
            else "—"
        )
        title = html.escape(str(plan.channel_title))
        if channel_link:
            channel_line = (
                f'Канал: <a href="{channel_link}">{title}</a> | Автор: {author}'
            )
        else:
            channel_line = f"Канал: {title} | Автор: {author}"

        link_part = (
            f"\n🔗 Ссылка на пост {plan.result_link}" if plan.result_link else ""
        )
        timezone_text = html.escape(str(plan.timezone_code))
        text = (
            "✅ Пост успешно опубликован\n"
            f"{link_part}\n\n"
            f"📅 {plan.local_date_text} • 🕔 {plan.local_time_text} "
            f"({timezone_text})\n"
            f"👀 Доставлено: {int(plan.delivered_count)}/1\n"
            "Переслано: 0\n\n"
            f"{channel_line}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Редактировать",
                        callback_data=str(plan.callback_data),
                    )
                ]
            ]
        )

        try:
            await self.bot.send_message(
                chat_id=int(plan.owner_tg_user_id),
                text=text,
                disable_web_page_preview=True,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Canonical owner notice send failed publication_id={} error_type={}",
                int(plan.publication_id),
                type(exc).__name__,
            )
            return CanonicalPublicationOwnerNoticeExecution(
                publication_id=int(plan.publication_id),
                attempted=1,
                sent=0,
                failed=1,
            )

        return CanonicalPublicationOwnerNoticeExecution(
            publication_id=int(plan.publication_id),
            attempted=1,
            sent=1,
            failed=0,
        )
