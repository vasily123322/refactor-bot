from __future__ import annotations

from typing import Optional
from loguru import logger
from contextlib import suppress
from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession


class Notifier:
    """Отправка уведомлений пользователям и в админ‑лог.

    Сейчас: уведомление о достижении 80% месячной квоты токенов ИИ.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def notify_ai_tokens_80pct(
        self, channel_id: int, used: int, month_limit: int, pct: float
    ) -> None:
        """Уведомить владельца канала и лог‑канал админа при достижении ~80% квоты.
        Безопасна к ошибкам: все внешние вызовы внутри suppress.
        """
        try:
            from app.repositories.channels import ChannelsRepo
            from app.domain.models import Client
            from app.bot.bot_instance import bot
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            repo = ChannelsRepo(self.session)
            ch = await repo.get_by_id(channel_id)
            if not ch:
                return
            owner = await self.session.get(Client, int(getattr(ch, "owner_id", 0)))
            uid = int(getattr(owner, "tg_user_id", 0)) if owner else 0

            # Текст для владельца
            text = (
                "⚠️ 80% месячной квоты ИИ израсходовано.\n\n"
                "Можно пополнить токены:\n"
                "• 500k — 299 ₽\n"
                "• 1M — 499 ₽\n"
                "• 2M — 899 ₽\n\n"
                "Неиспользованные доп. токены не сгорают и переносятся в следующий месяц с Pro или Free."
            )
            admin_username = settings.admin_username or "vasilyiusii"
            admin_url = f"https://t.me/{admin_username}"
            import urllib.parse as _urlparse

            share_text = (
                "Здравствуйте! Хочу пополнить доп. токены (500k/1M/2M). "
                f"Канал ID: {getattr(ch, 'tg_chat_id', channel_id)}."
            )
            share_url = f"https://t.me/share/url?url={_urlparse.quote_plus(admin_url)}&text={_urlparse.quote_plus(share_text)}"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Написать админу", url=admin_url)],
                    [InlineKeyboardButton(text="Отправить заявку", url=share_url)],
                ]
            )

            with suppress(Exception):
                if uid:
                    await bot.send_message(uid, text, reply_markup=kb)

            # Попытка получить ссылку на канал (не критично)
            chan_link: Optional[str] = None
            with suppress(Exception):
                chat = (
                    await bot.get_chat(int(getattr(ch, "tg_chat_id", 0)))
                    if ch
                    else None
                )
                uname = getattr(chat, "username", None) if chat else None
                if uname:
                    chan_link = f"https://t.me/{uname}"

            # Лог в админ‑канал
            with suppress(Exception):
                from app.repositories.admin import AdminConfigRepo

                log_chat_id = await AdminConfigRepo(self.session).get_log_chat_id()
                if log_chat_id:
                    pct_int = int(pct * 100)
                    log_text = (
                        f"AI tokens 80%: channel={(chan_link or getattr(ch, 'tg_chat_id', channel_id))} "
                        f"used={used}/{int(month_limit)} (~{pct_int}%)"
                    )
                    with suppress(Exception):
                        await bot.send_message(
                            int(log_chat_id), log_text, disable_web_page_preview=True
                        )
        except Exception as e:
            logger.warning(f"Notifier: failed to send 80% notice: {e}")
