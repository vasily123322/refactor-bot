from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import AISource
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_lifecycle import (
    SourceLifecycleError,
    SourceLifecyclePatch,
    SourceLifecycleService,
)


def test_source_lifecycle_updates_normalized_and_legacy_ai_source_atomically() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                legacy = AISource(
                    channel_id=91,
                    source_type="rss",
                    source_value="https://example.com/feed.xml",
                    mode="summary",
                    enabled=True,
                    citation_enabled=True,
                )
                session.add(legacy)
                await session.commit()
                await session.refresh(legacy)

                connector = await SourcesRepo(session).create_connector(
                    channel_id=91,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    mode="summary",
                    citation_enabled=True,
                    reuse_policy="reference_only",
                    legacy_ai_source_id=legacy.id,
                )

                updated = await SourceLifecycleService(session).update(
                    channel_id=91,
                    connector_id=connector.id,
                    patch=SourceLifecyclePatch(
                        enabled=False,
                        mode="rewrite",
                        citation_enabled=False,
                        reuse_policy="rewrite_with_attribution",
                    ),
                )

                assert updated.enabled is False
                assert updated.mode == "rewrite"
                assert updated.citation_enabled is False
                assert updated.reuse_policy == "rewrite_with_attribution"

                legacy = await session.get(AISource, legacy.id)
                assert legacy is not None
                assert legacy.enabled is False
                assert legacy.mode == "rewrite"
                assert legacy.citation_enabled is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_source_lifecycle_is_channel_scoped() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=92,
                    kind="url",
                    value="https://example.com/article",
                )
                with pytest.raises(SourceLifecycleError, match="source not found"):
                    await SourceLifecycleService(session).update(
                        channel_id=999,
                        connector_id=connector.id,
                        patch=SourceLifecyclePatch(enabled=False),
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_source_lifecycle_rejects_unknown_policy_without_mutation() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=93,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    reuse_policy="reference_only",
                )
                with pytest.raises(SourceLifecycleError, match="unsupported reuse policy"):
                    await SourceLifecycleService(session).update(
                        channel_id=93,
                        connector_id=connector.id,
                        patch=SourceLifecyclePatch(reuse_policy="unsafe_copy_anything"),
                    )
                await session.refresh(connector)
                assert connector.reuse_policy == "reference_only"
        finally:
            await engine.dispose()

    asyncio.run(run())
