from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.repositories.content import ContentRepo, MediaAssetsRepo
from app.services.publication_bridge import LegacyPublicationBridge, PublicationBridgeError


def test_publication_bridge_validates_asset_but_persists_durable_asset_id() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                asset = await MediaAssetsRepo(session).create(
                    channel_id=601,
                    kind="photo",
                    telegram_file_id="telegram-photo-file-id",
                )
                document = PostDocument(
                    mode="rich",
                    blocks=[
                        {
                            "id": "photo",
                            "type": "image",
                            "asset_id": int(asset.id),
                            "caption": "Keep identity",
                        }
                    ],
                )
                item = await ContentRepo(session).create(channel_id=601, document=document)

                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id)
                )

                task = await session.get(PostTask, int(publication.legacy_post_task_id))
                assert task is not None
                assert task.payload["type"] == "rich_document"
                assert task.payload["_content_channel_id"] == 601
                stored_block = task.payload["post_document"]["blocks"][0]
                assert stored_block["asset_id"] == int(asset.id)
                assert "telegram_file_id" not in stored_block
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_bridge_rejects_foreign_asset_before_task_creation() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                foreign = await MediaAssetsRepo(session).create(
                    channel_id=602,
                    kind="photo",
                    telegram_file_id="foreign-photo",
                )
                item = await ContentRepo(session).create(
                    channel_id=603,
                    document=PostDocument(
                        mode="rich",
                        blocks=[
                            {
                                "id": "photo",
                                "type": "image",
                                "asset_id": int(foreign.id),
                            }
                        ],
                    ),
                )

                with pytest.raises(PublicationBridgeError, match="media asset not found"):
                    await LegacyPublicationBridge(session).queue(content_item_id=int(item.id))

                assert await session.get(PostTask, 1) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
