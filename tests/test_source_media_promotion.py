from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.source_media_promotion as promotion_module
from app.core.db import Base
from app.domain.content.models import MediaAsset
from app.domain.sources.models import SourceDocument
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_media_promotion import SourceMediaPromotionService
from app.services.telegram_media_upload import TelegramMediaUploadResult
from app.userbot.client import UserbotMedia
from app.userbot.media_download import DownloadedUserbotMedia


def test_source_media_promotion_is_channel_scoped_and_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        calls = {"download": 0, "upload": 0}

        async def fake_download(gateway, *, target, message_id, max_bytes=20 * 1024 * 1024):
            calls["download"] += 1
            assert target == -100123456
            assert message_id == 77
            return DownloadedUserbotMedia(
                data=b"photo-bytes",
                media=UserbotMedia(
                    kind="photo",
                    mime_type="image/jpeg",
                    size_bytes=11,
                    width=1200,
                    height=630,
                ),
            )

        class FakeUploadService:
            def __init__(self, bot) -> None:
                self.bot = bot

            async def upload(self, *, tg_user_id, kind, data, filename):
                calls["upload"] += 1
                assert tg_user_id == 9501
                assert kind == "photo"
                assert data == b"photo-bytes"
                assert filename.endswith(".jpg")
                return TelegramMediaUploadResult(
                    telegram_file_id="bot-file-id",
                    width=1200,
                    height=630,
                )

        monkeypatch.setattr(promotion_module, "download_message_media", fake_download)
        monkeypatch.setattr(promotion_module, "TelegramMediaUploadService", FakeUploadService)

        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9501, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009501, "Channel")
                channel_id = int(channel.id)
                connector = await SourcesRepo(session).create_connector(
                    channel_id=channel_id,
                    kind="telegram",
                    value="@source",
                )
                document, _ = await SourcesRepo(session).upsert_document(
                    connector=connector,
                    external_id="telegram:-100123456:77",
                    content="[Telegram photo]",
                    metadata={
                        "telegram_chat_id": -100123456,
                        "telegram_message_id": 77,
                        "telegram_media": {
                            "kind": "photo",
                            "mime_type": "image/jpeg",
                            "size_bytes": 11,
                        },
                    },
                )
                document_id = int(document.id)
                candidate = await SourcesRepo(session).ensure_candidate(
                    source_document_id=document_id,
                    channel_id=channel_id,
                )
                candidate_id = int(candidate.id)

                service = SourceMediaPromotionService(
                    session,
                    userbot_gateway=object(),
                    bot=object(),
                )
                first = await service.promote(
                    channel_id=channel_id,
                    candidate_id=candidate_id,
                    tg_user_id=9501,
                )
                first_id = int(first.id)
                second = await service.promote(
                    channel_id=channel_id,
                    candidate_id=candidate_id,
                    tg_user_id=9501,
                )

                assert first_id == int(second.id)
                assert first.kind == "photo"
                assert first.source == "telegram_source"
                assert first.telegram_file_id == "bot-file-id"
                assert first.meta == {
                    "source_document_id": document_id,
                    "source": "telegram",
                }
                assert calls == {"download": 1, "upload": 1}

                assets = list((await session.execute(select(MediaAsset))).scalars().all())
                assert len(assets) == 1
                stored_document = await session.get(SourceDocument, document_id)
                assert stored_document is not None
                assert stored_document.meta["media_asset_id"] == first_id
        finally:
            await engine.dispose()

    asyncio.run(run())
