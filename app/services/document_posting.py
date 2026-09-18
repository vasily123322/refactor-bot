from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.domain.content import (
    PostDocument,
    UnsupportedPostDocumentCapabilityError,
    validate_native_document_capabilities,
)
from app.services.posting import PostingService
from app.services.rich_media_assets import RichMediaAssetError, RichMediaAssetResolver
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


class DocumentPostingService(PostingService):
    """PostingService extension for versioned PostDocument/Rich Message delivery."""

    async def send_now(self, channel_id: int, payload: dict) -> list[int] | None:
        """Dispatch without persisting provider exception text in legacy logs.

        The historical PostingService logs ``str(exception)`` in its outer catch. The
        production scheduler uses this PostDocument-aware subclass, so keep the same
        forward-fallback/dispatch contract here while logging only exception types.
        """
        try:
            forward_chat = payload.get("forward_from_chat_id")
            forward_message = payload.get("forward_from_message_id")
            if forward_chat and forward_message:
                try:
                    message = await self._send_with_retry(
                        self.bot.forward_message,
                        chat_id=int(channel_id),
                        from_chat_id=int(forward_chat),
                        message_id=int(forward_message),
                    )
                    return [int(message.message_id)]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug(
                        "Document posting: forward fallback failed error_type={}",
                        type(exc).__name__,
                    )
            return await self._dispatch(int(channel_id), payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Document posting failed target_chat_id={} payload_type={} error_type={}",
                int(channel_id),
                str(payload.get("type") or ""),
                type(exc).__name__,
            )
            return None

    async def _task_channel_context(self, payload: dict[str, Any]) -> int | None:
        """Resolve the bounded historical/manual rich asset-channel marker."""
        raw_marker = payload.get("_content_channel_id")
        if raw_marker is None:
            return None
        try:
            channel_id = int(raw_marker)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TelegramRenderError("rich_document asset channel context is invalid") from exc
        if channel_id <= 0:
            raise TelegramRenderError("rich_document asset channel context is invalid")
        return channel_id

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
        disable_notification: bool | None = None,
    ) -> list[int]:
        """Send one canonical document with an optional runtime silent override.

        ``None`` preserves each existing adapter path: classic keeps its historical
        compatibility payload unchanged, while rich keeps the renderer's document-level
        value. A boolean explicitly overrides delivery silence for either path so
        canonical runtime intent can stay outside the immutable content document.
        """
        try:
            validate_native_document_capabilities(document)
        except UnsupportedPostDocumentCapabilityError as exc:
            raise TelegramRenderError(str(exc)) from exc
        render_document = await self._resolve_media_assets(
            document,
            asset_channel_id=asset_channel_id,
        )
        plan = TelegramRenderer().render(render_document)
        if plan.kind == "classic":
            if plan.classic_payload is None:
                raise TelegramRenderError("classic renderer returned no payload")
            payload = dict(plan.classic_payload)
            if disable_notification is not None:
                payload["silent"] = bool(disable_notification)
            result = await self.send_now(int(chat_id), payload)
            if not result:
                raise TelegramRenderError("classic document could not be delivered")
            return [int(value) for value in result]

        if plan.rich_message is None:
            raise TelegramRenderError("rich renderer returned no InputRichMessage")
        effective_silent = (
            plan.disable_notification
            if disable_notification is None
            else bool(disable_notification)
        )
        message = await self._send_with_retry(
            self.bot.send_rich_message,
            chat_id=int(chat_id),
            rich_message=plan.rich_message,
            reply_markup=plan.reply_markup,
            disable_notification=effective_silent,
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
        asset_channel_id = await self._task_channel_context(payload)
        return await self.send_document(
            int(channel_id),
            document,
            asset_channel_id=asset_channel_id,
        )