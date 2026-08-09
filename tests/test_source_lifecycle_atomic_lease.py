from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion_lease import SourceIngestionLeaseService
from app.services.source_lifecycle import (
    SourceLifecycleError,
    SourceLifecyclePatch,
    SourceLifecycleService,
)


def test_successful_lifecycle_update_releases_exclusive_lease() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=197,
                    kind="url",
                    value="https://example.com/lifecycle-success",
                    reuse_policy="reference_only",
                )

                updated = await SourceLifecycleService(session).update(
                    channel_id=197,
                    connector_id=int(connector.id),
                    patch=SourceLifecyclePatch(reuse_policy="summarize"),
                )

                assert updated.reuse_policy == "summarize"
                assert (
                    await SourceIngestionLeaseService(session).current(int(connector.id))
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_failed_lifecycle_validation_releases_exclusive_lease() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=198,
                    kind="rss",
                    value="https://example.com/lifecycle-failure.xml",
                    reuse_policy="reference_only",
                )

                with pytest.raises(SourceLifecycleError, match="unsupported reuse policy"):
                    await SourceLifecycleService(session).update(
                        channel_id=198,
                        connector_id=int(connector.id),
                        patch=SourceLifecyclePatch(reuse_policy="unsafe_copy_anything"),
                    )

                await session.refresh(connector)
                assert connector.reuse_policy == "reference_only"
                assert (
                    await SourceIngestionLeaseService(session).current(int(connector.id))
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
