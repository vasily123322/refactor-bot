from __future__ import annotations

from pathlib import Path

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import MediaAsset
from app.domain.sources.models import SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.telegram_media_upload import (
    TelegramMediaUploadError,
    TelegramMediaUploadService,
)
from app.userbot.client import UserbotGateway
from app.userbot.media_download import (
    UserbotMediaDownloadError,
    download_message_media,
)


_PROMOTABLE_KINDS = {"photo", "video", "animation", "audio", "voice_note"}
_EXTENSIONS = {
    "photo": ".jpg",
    "video": ".mp4",
    "animation": ".gif",
    "audio": ".mp3",
    "voice_note": ".ogg",
}


class SourceMediaPromotionError(RuntimeError):
    pass


class SourceMediaPromotionService:
    """Promote one Telegram source message into a reusable channel MediaAsset."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        userbot_gateway: UserbotGateway,
        bot: Bot,
    ) -> None:
        self.session = session
        self.userbot_gateway = userbot_gateway
        self.bot = bot
        self.repo = SourcesRepo(session)

    @staticmethod
    def _source_coordinates(document: SourceDocument) -> tuple[int, int, str]:
        metadata = dict(document.meta or {})
        media = metadata.get("telegram_media")
        if not isinstance(media, dict):
            raise SourceMediaPromotionError("source document has no Telegram media")
        kind = str(media.get("kind") or "").strip().lower()
        if kind not in _PROMOTABLE_KINDS:
            raise SourceMediaPromotionError("source media kind is not promotable")
        try:
            chat_id = int(metadata.get("telegram_chat_id") or 0)
            message_id = int(metadata.get("telegram_message_id") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SourceMediaPromotionError("source Telegram coordinates are invalid") from exc
        if chat_id == 0 or message_id <= 0:
            raise SourceMediaPromotionError("source Telegram coordinates are invalid")
        return chat_id, message_id, kind

    async def _existing_asset(
        self,
        *,
        document: SourceDocument,
        channel_id: int,
    ) -> MediaAsset | None:
        raw_id = dict(document.meta or {}).get("media_asset_id")
        try:
            asset_id = int(raw_id or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if asset_id <= 0:
            return None
        asset = await self.session.get(MediaAsset, asset_id)
        if asset is None or int(asset.channel_id) != int(channel_id):
            return None
        return asset

    async def promote(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        tg_user_id: int,
    ) -> MediaAsset:
        candidate = await self.repo.get_candidate_for_channel(candidate_id, channel_id)
        if candidate is None or str(candidate.status) != "new":
            raise SourceMediaPromotionError("candidate not found")
        document = await self.session.get(SourceDocument, int(candidate.source_document_id))
        if document is None or int(document.channel_id) != int(channel_id):
            raise SourceMediaPromotionError("source document not found")
        existing = await self._existing_asset(document=document, channel_id=channel_id)
        if existing is not None:
            return existing
        chat_id, message_id, expected_kind = self._source_coordinates(document)
        document_id = int(document.id)

        # Do not keep a database transaction open around Telegram network I/O.
        await self.session.rollback()
        try:
            downloaded = await download_message_media(
                self.userbot_gateway,
                target=chat_id,
                message_id=message_id,
            )
        except UserbotMediaDownloadError as exc:
            raise SourceMediaPromotionError(str(exc)) from exc

        actual_kind = str(downloaded.media.kind).lower()
        if actual_kind != expected_kind:
            raise SourceMediaPromotionError("source media changed since ingestion")
        if actual_kind not in _PROMOTABLE_KINDS:
            raise SourceMediaPromotionError("source media kind is not promotable")

        filename = Path(f"source-{document_id}-{message_id}{_EXTENSIONS[actual_kind]}").name
        try:
            uploaded = await TelegramMediaUploadService(self.bot).upload(
                tg_user_id=int(tg_user_id),
                kind=actual_kind,
                data=downloaded.data,
                filename=filename,
            )
        except TelegramMediaUploadError as exc:
            raise SourceMediaPromotionError("Telegram media promotion failed") from exc

        try:
            result = await self.session.execute(
                select(SourceDocument)
                .where(
                    SourceDocument.id == document_id,
                    SourceDocument.channel_id == int(channel_id),
                )
                .with_for_update()
            )
            locked_document = result.scalar_one_or_none()
            if locked_document is None:
                raise SourceMediaPromotionError("source document not found")
            existing = await self._existing_asset(
                document=locked_document,
                channel_id=channel_id,
            )
            if existing is not None:
                await self.session.rollback()
                return existing

            asset = MediaAsset(
                channel_id=int(channel_id),
                kind=actual_kind,
                source="telegram_source",
                telegram_file_id=uploaded.telegram_file_id,
                mime_type=downloaded.media.mime_type,
                width=uploaded.width or downloaded.media.width,
                height=uploaded.height or downloaded.media.height,
                duration_seconds=(
                    uploaded.duration_seconds or downloaded.media.duration_seconds
                ),
                size_bytes=len(downloaded.data),
                meta={
                    "source_document_id": document_id,
                    "source": "telegram",
                },
            )
            self.session.add(asset)
            await self.session.flush()
            locked_document.meta = {
                **dict(locked_document.meta or {}),
                "media_asset_id": int(asset.id),
            }
            await self.session.commit()
            await self.session.refresh(asset)
            return asset
        except SourceMediaPromotionError:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            raise SourceMediaPromotionError("source media promotion persistence failed") from exc
