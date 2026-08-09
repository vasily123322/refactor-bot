from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.repositories.content import MediaAssetsRepo
from app.services.document_posting import DocumentPostingService


@dataclass
class _Message:
    message_id: int


class _Bot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_rich_message(self, **kwargs):
        self.calls.append(kwargs)
        return _Message(message_id=901)


def test_document_posting_resolves_asset_id_only_at_delivery_edge() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                asset = await MediaAssetsRepo(session).create(
                    channel_id=701,
                    kind="video",
                    telegram_file_id="delivery-video-file-id",
                    width=1920,
                    height=1080,
                )

            document = PostDocument(
                mode="rich",
                blocks=[
                    {
                        "id": "video",
                        "type": "media",
                        "kind": "video",
                        "asset_id": int(asset.id),
                    }
                ],
            )
            bot = _Bot()
            posting = DocumentPostingService(bot, Session)  # type: ignore[arg-type]

            ids = await posting.send_document(
                123456,
                document,
                asset_channel_id=701,
            )

            assert ids == [901]
            assert len(bot.calls) == 1
            rich_block = bot.calls[0]["rich_message"].blocks[0]
            assert rich_block.video.media == "delivery-video-file-id"
            assert rich_block.video.width == 1920
            assert rich_block.video.height == 1080
            assert document.to_dict()["blocks"][0]["asset_id"] == int(asset.id)
        finally:
            await engine.dispose()

    asyncio.run(run())
