from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.repositories.content import MediaAssetsRepo
from app.services.rich_media_assets import (
    MAX_RICH_MEDIA_ASSETS_PER_DOCUMENT,
    RichMediaAssetError,
    RichMediaAssetResolver,
)


def test_resolver_keeps_asset_identity_in_input_and_builds_transport_copy() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                asset = await MediaAssetsRepo(session).create(
                    channel_id=501,
                    kind="video",
                    telegram_file_id="video-file-id",
                    width=1280,
                    height=720,
                    duration_seconds=42,
                )
                document = PostDocument(
                    mode="rich",
                    blocks=[
                        {
                            "id": "media",
                            "type": "media",
                            "kind": "video",
                            "asset_id": int(asset.id),
                            "caption": "Durable asset",
                        }
                    ],
                )

                resolved = await RichMediaAssetResolver(session).resolve(
                    document,
                    channel_id=501,
                )

                original = document.to_dict()["blocks"][0]
                rendered = resolved.to_dict()["blocks"][0]
                assert original["asset_id"] == int(asset.id)
                assert "telegram_file_id" not in original
                assert rendered["telegram_file_id"] == "video-file-id"
                assert "asset_id" not in rendered
                assert rendered["width"] == 1280
                assert rendered["height"] == 720
                assert rendered["duration"] == 42
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_resolver_foreign_and_missing_assets_fail_with_same_error() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                foreign = await MediaAssetsRepo(session).create(
                    channel_id=777,
                    kind="photo",
                    telegram_file_id="foreign-photo",
                )
                resolver = RichMediaAssetResolver(session)

                for asset_id in (int(foreign.id), int(foreign.id) + 9999):
                    document = PostDocument(
                        mode="rich",
                        blocks=[{"id": "p", "type": "image", "asset_id": asset_id}],
                    )
                    with pytest.raises(RichMediaAssetError) as captured:
                        await resolver.resolve(document, channel_id=778)
                    assert str(captured.value) == "media asset not found"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_resolver_rejects_kind_mismatch_and_transportless_asset() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = MediaAssetsRepo(session)
                photo = await repo.create(
                    channel_id=502,
                    kind="photo",
                    telegram_file_id="photo-file-id",
                )
                empty = await repo.create(channel_id=502, kind="video")
                resolver = RichMediaAssetResolver(session)

                with pytest.raises(RichMediaAssetError, match="kind does not match"):
                    await resolver.resolve(
                        PostDocument(
                            mode="rich",
                            blocks=[
                                {
                                    "id": "v",
                                    "type": "media",
                                    "kind": "video",
                                    "asset_id": int(photo.id),
                                }
                            ],
                        ),
                        channel_id=502,
                    )

                with pytest.raises(RichMediaAssetError, match="no transport reference"):
                    await resolver.resolve(
                        PostDocument(
                            mode="rich",
                            blocks=[
                                {
                                    "id": "v2",
                                    "type": "media",
                                    "kind": "video",
                                    "asset_id": int(empty.id),
                                }
                            ],
                        ),
                        channel_id=502,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_resolver_bounds_number_of_asset_ids_before_query() -> None:
    async def run() -> None:
        document = PostDocument(
            mode="rich",
            blocks=[
                {
                    "id": f"asset-{index}",
                    "type": "image",
                    "asset_id": index + 1,
                }
                for index in range(MAX_RICH_MEDIA_ASSETS_PER_DOCUMENT + 1)
            ],
        )
        resolver = RichMediaAssetResolver(object())  # type: ignore[arg-type]
        with pytest.raises(RichMediaAssetError, match="more than 100 media assets"):
            await resolver.resolve(document, channel_id=503)

    asyncio.run(run())
