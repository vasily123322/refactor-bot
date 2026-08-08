from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision, MediaAsset
from app.repositories.content import ContentRepo, MediaAssetsRepo


def test_content_models_are_registered_in_global_metadata() -> None:
    assert "content_items" in Base.metadata.tables
    assert "content_revisions" in Base.metadata.tables
    assert "media_assets" in Base.metadata.tables


def test_content_repo_creates_immutable_revisions() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(ContentItem.__table__.create)
                await conn.run_sync(ContentRevision.__table__.create)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = ContentRepo(session)
                item = await repo.create(
                    channel_id=7,
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "v1"}]
                    ),
                    title="Draft",
                    created_by_tg_user_id=100,
                )

                assert item.current_revision == 1
                assert item.status == "draft"
                first = await repo.get_document(item.id)
                assert first is not None
                assert first.primary_text() == "v1"

                second_row = await repo.append_revision(
                    item.id,
                    PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "v2"}]
                    ),
                    created_by_tg_user_id=100,
                    source="ai_rewrite",
                    status="ready",
                )
                assert second_row.revision == 2

                current = await repo.get(item.id)
                assert current is not None
                assert current.current_revision == 2
                assert current.status == "ready"
                assert (await repo.get_document(item.id, 1)).primary_text() == "v1"
                assert (await repo.get_document(item.id, 2)).primary_text() == "v2"
                assert await repo.get_for_channel(item.id, 7) is not None
                assert await repo.get_for_channel(item.id, 8) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_content_repo_imports_legacy_payload_losslessly() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(ContentItem.__table__.create)
                await conn.run_sync(ContentRevision.__table__.create)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = ContentRepo(session)
                item = await repo.create_from_legacy_payload(
                    channel_id=1,
                    payload={
                        "type": "photo",
                        "file_id": "tg-file",
                        "caption": "Caption",
                        "repeat_seconds": 3600,
                    },
                )
                document = await repo.get_document(item.id)
                assert document is not None
                assert document.blocks[0]["type"] == "photo"
                assert document.blocks[0]["file_id"] == "tg-file"
                assert document.metadata["legacy_payload_extra"]["repeat_seconds"] == 3600
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_media_assets_repo_keeps_reusable_telegram_reference() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(MediaAsset.__table__.create)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = MediaAssetsRepo(session)
                asset = await repo.create(
                    channel_id=12,
                    kind="photo",
                    telegram_file_id="file-id",
                    width=1080,
                    height=1080,
                    metadata={"alt": "cover"},
                )
                assert asset.telegram_file_id == "file-id"
                items = await repo.list_by_channel(12)
                assert [row.id for row in items] == [asset.id]
                assert items[0].meta["alt"] == "cover"
        finally:
            await engine.dispose()

    asyncio.run(run())
