from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    CanonicalPublicationLiveAdminLogPlan,
    CanonicalPublicationLiveOwnerNoticePlan,
)


_TELEGRAM_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class CanonicalPublicationLiveAuxiliaryBot(Protocol):
    async def get_chat(self, chat_id: int): ...

    async def create_chat_invite_link(self, **kwargs): ...

    async def send_message(self, chat_id: int, text: str, **kwargs): ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryLiveAuxiliaryExecution:
    publication_id: int
    admin_attempted: int = 0
    admin_sent: int = 0
    admin_failed: int = 0
    owner_attempted: int = 0
    owner_sent: int = 0
    owner_failed: int = 0
    invalid_plans: int = 0


def _telegram_username(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lstrip("@")
    return text if _TELEGRAM_USERNAME.fullmatch(text) else None


def _telegram_invite_link(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > 2048
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in text)
    ):
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "t.me"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    if not (parsed.path.startswith("/+") or parsed.path.startswith("/joinchat/")):
        return None
    token = parsed.path.split("/", 2)[-1].lstrip("+")
    if not token or len(token) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    return f"https://t.me{parsed.path}"


class CanonicalPublicationDeliveryLiveAuxiliaryExecutor:
    """Execute live owner/admin auxiliary plans once, without database access.

    Legacy ordering is preserved: optional admin logging runs first, then the owner
    published notice. Both operations are best-effort and never change primary delivery
    success. Cancellation remains authoritative because replaying either side effect can
    create duplicates; callers must leave the primary claim ambiguous for recovery.

    This executor is deliberately not a retry API. It accepts only the immutable plans
    produced while the exact canonical delivery lease is live.
    """

    def __init__(self, bot: CanonicalPublicationLiveAuxiliaryBot) -> None:
        self.bot = bot

    async def execute(
        self,
        plan: CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    ) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        publication_id = int(plan.publication_id)
        invalid_plans = 0

        admin_attempted = 0
        admin_sent = 0
        admin_failed = 0
        if plan.admin_log is not None:
            if int(plan.admin_log.publication_id) != publication_id:
                invalid_plans += 1
            else:
                admin_attempted = 1
                admin_sent, admin_failed = await self._execute_admin(plan.admin_log)

        owner_attempted = 0
        owner_sent = 0
        owner_failed = 0
        if plan.owner_notice is not None:
            if int(plan.owner_notice.publication_id) != publication_id:
                invalid_plans += 1
            else:
                owner_attempted = 1
                owner_sent, owner_failed = await self._execute_owner(plan.owner_notice)

        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=publication_id,
            admin_attempted=admin_attempted,
            admin_sent=admin_sent,
            admin_failed=admin_failed,
            owner_attempted=owner_attempted,
            owner_sent=owner_sent,
            owner_failed=owner_failed,
            invalid_plans=invalid_plans,
        )

    async def _execute_admin(
        self,
        plan: CanonicalPublicationLiveAdminLogPlan,
    ) -> tuple[int, int]:
        post_link = plan.result_link
        channel_link: str | None = None
        chat = None
        try:
            chat = await self.bot.get_chat(int(plan.source_telegram_chat_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Canonical live admin log chat lookup failed publication_id={} "
                "error_type={}",
                int(plan.publication_id),
                type(exc).__name__,
            )

        source_username = _telegram_username(
            getattr(chat, "username", None) if chat is not None else None
        )
        if post_link is None:
            if source_username:
                post_link = (
                    f"https://t.me/{source_username}/{int(plan.primary_message_id)}"
                )
            else:
                cid = str(int(plan.source_telegram_chat_id))
                if cid.startswith("-100") and cid[4:].isdigit() and int(cid[4:]) > 0:
                    post_link = (
                        f"https://t.me/c/{cid[4:]}/{int(plan.primary_message_id)}"
                    )

        if source_username:
            channel_link = f"https://t.me/{source_username}"
        else:
            try:
                invite = await self.bot.create_chat_invite_link(
                    chat_id=int(plan.source_telegram_chat_id),
                    name="post-log",
                    creates_join_request=False,
                )
                channel_link = _telegram_invite_link(
                    getattr(invite, "invite_link", None)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "Canonical live admin log invite lookup failed publication_id={} "
                    "error_type={}",
                    int(plan.publication_id),
                    type(exc).__name__,
                )

        author_username = _telegram_username(plan.author_username)
        if author_username:
            escaped_username = html.escape(author_username)
            user_html = (
                f'<a href="https://t.me/{author_username}">@{escaped_username}</a>'
            )
        elif plan.author_tg_user_id:
            label = html.escape(str(plan.author_full_name or int(plan.author_tg_user_id)))
            user_html = (
                f'<a href="tg://user?id={int(plan.author_tg_user_id)}">{label}</a>'
            )
        else:
            user_html = "пользователь"

        text = f"пользователь {user_html} отправил пост: "
        if post_link:
            text += f'<a href="{html.escape(post_link, quote=True)}">ссылка</a>'
        else:
            text += "(без ссылки)"
        text += " в канал/чат: "
        if channel_link:
            text += f'<a href="{html.escape(channel_link, quote=True)}">перейти</a>'
        else:
            text += str(int(plan.source_telegram_chat_id))

        try:
            await self.bot.send_message(
                int(plan.log_chat_id),
                text,
                disable_web_page_preview=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Canonical live admin log send failed publication_id={} error_type={}",
                int(plan.publication_id),
                type(exc).__name__,
            )
            return 0, 1
        return 1, 0

    async def _execute_owner(
        self,
        plan: CanonicalPublicationLiveOwnerNoticePlan,
    ) -> tuple[int, int]:
        channel_link: str | None = None
        try:
            chat = await self.bot.get_chat(int(plan.source_telegram_chat_id))
            username = _telegram_username(getattr(chat, "username", None))
            if username:
                channel_link = f"https://t.me/{username}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Canonical live owner notice channel lookup failed publication_id={} "
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
            f"\n🔗 Ссылка на пост {html.escape(plan.result_link)}"
            if plan.result_link
            else ""
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
                "Canonical live owner notice send failed publication_id={} error_type={}",
                int(plan.publication_id),
                type(exc).__name__,
            )
            return 0, 1
        return 1, 0
