from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion_lease import SourceIngestionLeaseService


def test_source_ingestion_lease_is_exclusive_stale_recoverable_and_token_checked() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=71,
                    kind="url",
                    value="https://example.com/lease",
                )
                connector_id = int(connector.id)

            now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            async with Session() as first_session:
                first = await SourceIngestionLeaseService(first_session).acquire(
                    connector_id=connector_id,
                    holder="worker",
                    ttl_seconds=30,
                    now=now,
                )
                assert first is not None

            async with Session() as second_session:
                blocked = await SourceIngestionLeaseService(second_session).acquire(
                    connector_id=connector_id,
                    holder="studio",
                    ttl_seconds=30,
                    now=now + timedelta(seconds=10),
                )
                assert blocked is None

            async with Session() as takeover_session:
                takeover_service = SourceIngestionLeaseService(takeover_session)
                second = await takeover_service.acquire(
                    connector_id=connector_id,
                    holder="studio",
                    ttl_seconds=30,
                    now=now + timedelta(seconds=31),
                )
                assert second is not None
                assert second.lease_token != first.lease_token
                assert second.holder == "studio"

                # A delayed finally block from the expired lease must not delete the
                # replacement lease acquired by another task/process.
                assert await takeover_service.release(first) is False
                current = await takeover_service.current(connector_id)
                assert current is not None
                assert current.lease_token == second.lease_token
                assert await takeover_service.release(second) is True
                assert await takeover_service.current(connector_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
