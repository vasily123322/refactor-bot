from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from loguru import logger

from app.services.canonical_publication_admin_log_planner import (
    CanonicalPublicationAdminLogPlan,
)


_TELEGRAM_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class CanonicalAdminLogBot(Protocol):
    async def get_chat(self, chat_id: int): ...

    async def create_chat_invite_link(self, **kwargs): ...

    async def send_message(self, chat_id: int, text: str, **kwargs): ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAdminLogExecution:
    publication_id: int
    attempted: int
    sent: int
    failed: int


def _telegram_username(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lstrip("@")
    return text if _TELEGRAM_USERNAME.fullmatch(text) else None


def _telegram_invite_link(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 2048 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
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


class CanonicalPublicationAdminLogExecutor:
    """Send one proven admin log with legacy-compatible best-effort semantics.

    Historical logging may create a private-channel invite link when no public username
    exists. That provider side effect is preserved for parity, but all provider-derived
    URL identities are validated before entering HTML. This executor is a one-shot
    auxiliary primitive: repeating it may create another invite link and duplicate log
    message, so a future coordinator must not retry it without durable completion state.
    """

    def __init__(self, bot: CanonicalAdminLogBot) -> None:
        self.bot = bot

    async def execute(
        self,
        plan: CanonicalPublicationAdminLogPlan,
    ) -> CanonicalPublicationAdminLogExecution:
        post_link = plan.result_link
        channel_link: str | None = None
        chat = None
        try:
            chat = await self.bot.get_chat(int(plan.source_telegram_chat_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Canonical admin log chat lookup failed publication_id={} error_type={}",
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
                    "Canonical admin log invite lookup failed publication_id={} "
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
                "Canonical admin log send failed publication_id={} error_type={}",
                int(plan.publication_id),
                type(exc).__name__,
            )
            return CanonicalPublicationAdminLogExecution(
                publication_id=int(plan.publication_id),
                attempted=1,
                sent=0,
                failed=1,
            )

        return CanonicalPublicationAdminLogExecution(
            publication_id=int(plan.publication_id),
            attempted=1,
            sent=1,
            failed=0,
        )
