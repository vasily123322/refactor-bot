from __future__ import annotations

import asyncio
from typing import ClassVar

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.source_ingestion as worker_module
from app.core.db import Base
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import IngestionResult
from app.services.source_ingestion_lease import SourceIngestionLeaseService
from app.services.source_worker_policy import source_worker_failure_count
from app.workers.source_ingestion import SourceIngestionWorker


class _RecordingService:
    calls: ClassVar[list[int]] = []

    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        type(self).calls.append(int(connector.id))
        return IngestionResult(int(connector.id), 0, 0, 0)


def test_worker_busy_lease_skips_without_opening_failure_circuit(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=72,
                    kind="url",
                    value="https://example.com/busy",
                )
                connector_id = int(connector.id)
                manual_lease = await SourceIngestionLeaseService(session).acquire(
                    connector_id=connector_id,
                    holder="studio",
                )
                assert manual_lease is not None

            _RecordingService.calls = []
            monkeypatch.setattr(worker_module, "SourceIngestionService", _RecordingService)
            worker = SourceIngestionWorker(
                session_factory=Session,
                interval_seconds=15,
                max_connectors_per_tick=1,
            )
            assert await worker.run_once() == 0
            assert _RecordingService.calls == []

            async with Session() as session:
                row = await session.get(worker_module.SourceConnector, connector_id)
                assert row is not None
                assert source_worker_failure_count(row) == 0
                assert await SourceIngestionLeaseService(session).release(manual_lease) is True

            assert await worker.run_once() == 1
            assert _RecordingService.calls == [connector_id]
        finally:
            await engine.dispose()

    asyncio.run(run())
