from __future__ import annotations

from typing import Any

from app.domain.content import PostDocument
from app.services.posting import PostingService
from app.services.rich_media_assets import RichMediaAssetError, RichMediaAssetResolver
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


class DocumentPostingService(PostingService):
    """PostingService extension for versioned PostDocument/Rich Message delivery."""

    async def _resolve_media_assets(
        self,
        document: PostDocument,
        *,
        asset_channel_id: int | None,
    ) -> PostDocument:
        if asset_channel_id is None:
            return document
        try:
            if self.session_factory is not None:
                async with self.session_factory() as session:
                    return await RichMediaAssetResolver(session).resolve(
                        document,
                        channel_id=int(asset_channel_id),
                    )
            if self.session is not None:
                return await RichMediaAssetResolver(self.session).resolve(
                    document,
                    channel_id=int(asset_channel_id),
                )
        except RichMediaAssetError as exc:
            raise TelegramRenderError(str(exc)) from exc
        raise TelegramRenderError("media asset resolution requires a database session")

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        render_document = await self._resolve_media_assets(
            document,
            asset_channel_id=asset_channel_id,
        )
        plan = TelegramRenderer().render(render_document)
        if plan.kind == "classic":
            if plan.classic_payload is None:
                raise TelegramRenderError("classic renderer returned no payload")
            result = await self.send_now(int(chat_id), dict(plan.classic_payload))
            if not result:
                raise TelegramRenderError("classic document could not be delivered")
            return [int(value) for value in result]

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

    async def _dispatch(self, channel_id: int, payload: dict[str, Any]) -> list[int]:
        if str(payload.get("type") or "") != "rich_document":
            return await super()._dispatch(channel_id, payload)

        raw_document = payload.get("post_document")
        if not isinstance(raw_document, dict):
            raise TelegramRenderError("rich_document task requires post_document")
        document = PostDocument.from_dict(raw_document)
        if document.mode != "rich":
            raise TelegramRenderError("rich_document task requires rich PostDocument")
        raw_asset_channel_id = payload.get("_content_channel_id")
        asset_channel_id = (
            int(raw_asset_channel_id) if raw_asset_channel_id is not None else None
        )
        return await self.send_document(
            int(channel_id),
            document,
            asset_channel_id=asset_channel_id,
        )
