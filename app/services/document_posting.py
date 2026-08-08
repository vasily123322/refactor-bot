from __future__ import annotations

from typing import Any

from app.domain.content import PostDocument
from app.services.posting import PostingService
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


class DocumentPostingService(PostingService):
    """PostingService extension for versioned PostDocument/Rich Message delivery."""

    async def send_document(self, chat_id: int, document: PostDocument) -> list[int]:
        plan = TelegramRenderer().render(document)
        if plan.kind == "classic":
            if plan.classic_payload is None:
                raise TelegramRenderError("classic renderer returned no payload")
            return await self.send_now(int(chat_id), dict(plan.classic_payload))

        if plan.rich_message is None:
            raise TelegramRenderError("rich renderer returned no InputRichMessage")
        message = await self._send_with_retry(
            self.bot.send_rich_message,
            chat_id=int(chat_id),
            rich_message=plan.rich_message,
            reply_markup=plan.reply_markup,
            disable_notification=plan.disable_notification,
            protect_content=plan.protect_content,
        )
        return [int(message.message_id)]

    async def _dispatch(
        self,
        pl: dict[str, Any],
        tg_chat_id: int,
        ch_settings=None,
    ) -> list[int]:
        if str(pl.get("type") or "") != "rich_document":
            return await super()._dispatch(pl, tg_chat_id, ch_settings)

        raw_document = pl.get("post_document")
        if not isinstance(raw_document, dict):
            raise TelegramRenderError("rich_document task requires post_document")
        document = PostDocument.from_dict(raw_document)
        if document.mode != "rich":
            raise TelegramRenderError("rich_document task requires rich PostDocument")
        return await self.send_document(int(tg_chat_id), document)
