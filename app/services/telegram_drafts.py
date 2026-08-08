from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from loguru import logger


_MAX_DRAFT_CHARS = 4096


def _clip_draft_text(text: str) -> str:
    value = str(text or "")
    if len(value) <= _MAX_DRAFT_CHARS:
        return value
    return value[: _MAX_DRAFT_CHARS - 1] + "…"


class TelegramDraftStreamer:
    """Render generated text as a Telegram ephemeral draft with safe fallback.

    Draft updates are throttled to avoid one Bot API request per model token.
    Partial Markdown/HTML is intentionally sent as plain text because chunks may
    contain incomplete formatting constructs.
    """

    def __init__(
        self,
        bot: Any,
        *,
        chat_id: int,
        seed: int = 0,
        min_interval_seconds: float = 0.45,
        min_new_chars: int = 48,
    ) -> None:
        self.bot = bot
        self.chat_id = int(chat_id)
        raw_id = (time.time_ns() ^ (self.chat_id << 8) ^ int(seed or 0)) & 0x7FFFFFFF
        self.draft_id = raw_id or 1
        self.min_interval_seconds = max(0.1, float(min_interval_seconds))
        self.min_new_chars = max(1, int(min_new_chars))
        self._draft_available = True
        self._fallback_message: Any | None = None
        self._last_sent_text = ""
        self._last_sent_at = 0.0

    async def start(self) -> None:
        try:
            await self.bot.send_message_draft(
                chat_id=self.chat_id,
                draft_id=self.draft_id,
                text="",
            )
            self._last_sent_at = time.monotonic()
        except Exception as exc:
            logger.debug("Telegram draft unavailable, using editable message: {!r}", exc)
            self._draft_available = False
            await self._ensure_fallback()

    async def _ensure_fallback(self) -> Any:
        if self._fallback_message is None:
            self._fallback_message = await self.bot.send_message(
                chat_id=self.chat_id,
                text="⏳ Генерирую…",
            )
        return self._fallback_message

    def _should_flush(self, text: str, *, force: bool) -> bool:
        if force:
            return True
        if not text or text == self._last_sent_text:
            return False
        new_chars = max(0, len(text) - len(self._last_sent_text))
        elapsed = time.monotonic() - self._last_sent_at
        return new_chars >= self.min_new_chars or elapsed >= self.min_interval_seconds

    async def update(self, full_text: str, *, force: bool = False) -> None:
        text = _clip_draft_text(full_text)
        if not self._should_flush(text, force=force):
            return

        if self._draft_available:
            try:
                await self.bot.send_message_draft(
                    chat_id=self.chat_id,
                    draft_id=self.draft_id,
                    text=text,
                )
                self._last_sent_text = text
                self._last_sent_at = time.monotonic()
                return
            except Exception as exc:
                logger.debug("Telegram draft update failed, switching fallback: {!r}", exc)
                self._draft_available = False

        fallback = await self._ensure_fallback()
        try:
            await fallback.edit_text(text or "⏳ Генерирую…")
        except Exception:
            # A fallback edit is UX-only and must never abort an LLM response.
            logger.debug("Telegram fallback draft edit failed", exc_info=True)
            return
        self._last_sent_text = text
        self._last_sent_at = time.monotonic()

    async def finish(self, full_text: str) -> None:
        await self.update(full_text, force=True)

    async def cleanup(self) -> None:
        if self._fallback_message is not None:
            with suppress(Exception):
                await self._fallback_message.delete()
            self._fallback_message = None
