from __future__ import annotations

import asyncio
import re
from typing import Protocol

from loguru import logger

from app.services.telegram_results import (
    normalize_telegram_message_ids,
    normalize_telegram_result_link,
    rewrite_telegram_result_link_message_id,
)


_TELEGRAM_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class CanonicalPublicationResultLinkBot(Protocol):
    async def get_chat(self, chat_id: int): ...


class CanonicalPublicationResultLinkResolver:
    """Best-effort Telegram post-link enrichment for one successful primary delivery.

    Private/supergroup ``-100...`` links are deterministic and require no provider
    lookup. Public links require a channel username from ``get_chat``; provider failures
    or malformed usernames simply produce no link and never change primary success.
    """

    def __init__(self, bot: CanonicalPublicationResultLinkBot) -> None:
        self.bot = bot

    async def resolve(
        self,
        *,
        chat_id: int,
        message_ids,
    ) -> str | None:
        ids = normalize_telegram_message_ids(message_ids)
        if not ids:
            return None
        safe_chat_id = int(chat_id)
        message_id = int(ids[-1])

        private_link = rewrite_telegram_result_link_message_id(
            None,
            chat_id=safe_chat_id,
            message_id=message_id,
        )
        if private_link is not None:
            return private_link

        try:
            chat = await self.bot.get_chat(safe_chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Canonical publication result link lookup failed chat_id={} error_type={}",
                safe_chat_id,
                type(exc).__name__,
            )
            return None

        raw_username = getattr(chat, "username", None)
        if raw_username is None:
            return None
        username = str(raw_username).strip().lstrip("@")
        if not _TELEGRAM_USERNAME.fullmatch(username):
            return None
        return normalize_telegram_result_link(
            f"https://t.me/{username}/{message_id}"
        )
