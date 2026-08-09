from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import AISource, GrabSource
from app.repositories.sources_v2 import SourcesRepo
from app.services.legacy_source_mirror import LegacySourceMirror
from app.services.source_ingestion_lease import SourceIngestionLeaseService
from app.services.source_lifecycle import (
    SourceLifecycleBusy,
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


def test_legacy_grab_mirror_is_read_only_in_sources_lifecycle() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                grab = GrabSource(
                    source_chat_id=-100777,
                    target_channel_id=94,
                    filter_flags={"text": 1},
                )
                session.add(grab)
                await session.commit()
                await session.refresh(grab)
                await LegacySourceMirror(session).sync_channel(94)
                connector = (await SourcesRepo(session).list_connectors(94))[0]
                assert connector.legacy_grab_source_id == grab.id

                with pytest.raises(SourceLifecycleError, match="read-only"):
                    await SourceLifecycleService(session).update(
                        channel_id=94,
                        connector_id=connector.id,
                        patch=SourceLifecyclePatch(enabled=False),
                    )
                await session.refresh(connector)
                assert connector.enabled is True
                assert connector.reuse_policy == "reference_only"
                assert "enabled" not in GrabSource.__table__.columns
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_active_ingestion_lease_blocks_lifecycle_without_mutation() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                legacy = AISource(
                    channel_id=95,
                    source_type="rss",
                    source_value="https://example.com/busy.xml",
                    mode="summary",
                    enabled=True,
                    citation_enabled=True,
                )
                session.add(legacy)
                await session.commit()
                await session.refresh(legacy)
                connector = await SourcesRepo(session).create_connector(
                    channel_id=95,
                    kind="rss",
                    value="https://example.com/busy.xml",
                    mode="summary",
                    citation_enabled=True,
                    reuse_policy="reference_only",
                    legacy_ai_source_id=legacy.id,
                )
                lease = await SourceIngestionLeaseService(session).acquire(
                    connector_id=int(connector.id),
                    holder="worker",
                )
                assert lease is not None

                with pytest.raises(SourceLifecycleBusy, match="ingestion is running"):
                    await SourceLifecycleService(session).update(
                        channel_id=95,
                        connector_id=connector.id,
                        patch=SourceLifecyclePatch(
                            enabled=False,
                            mode="rewrite",
                            citation_enabled=False,
                            reuse_policy="rewrite_with_attribution",
                        ),
                    )

                await session.refresh(connector)
                await session.refresh(legacy)
                assert connector.enabled is True
                assert connector.mode == "summary"
                assert connector.citation_enabled is True
                assert connector.reuse_policy == "reference_only"
                assert legacy.enabled is True
                assert legacy.mode == "summary"
                assert legacy.citation_enabled is True
                assert await SourceIngestionLeaseService(session).release(lease) is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_ingestion_lease_does_not_block_lifecycle_update() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=96,
                    kind="url",
                    value="https://example.com/expired",
                    reuse_policy="reference_only",
                )
                expired = await SourceIngestionLeaseService(session).acquire(
                    connector_id=int(connector.id),
                    holder="worker",
                    ttl_seconds=30,
                    now=datetime(2020, 1, 1, tzinfo=timezone.utc),
                )
                assert expired is not None

                updated = await SourceLifecycleService(session).update(
                    channel_id=96,
                    connector_id=connector.id,
                    patch=SourceLifecyclePatch(reuse_policy="summarize"),
                )
                assert updated.reuse_policy == "summarize"
        finally:
            await engine.dispose()

    asyncio.run(run())
