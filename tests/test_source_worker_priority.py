from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.source_ingestion as worker_module
from app.core.db import Base
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import IngestionResult
from app.services.telegram_source_ingestion import TELEGRAM_BACKLOG_HINT_KEY
from app.workers.source_ingestion import SourceIngestionWorker


class _RecordingService:
    calls: ClassVar[list[int]] = []

    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        type(self).calls.append(int(connector.id))
        return IngestionResult(int(connector.id), 0, 0, 0)


def test_worker_prioritizes_backlog_inside_window_without_breaking_rotation(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            async with Session() as session:
                first = await SourcesRepo(session).create_connector(
                    channel_id=81,
                    kind="url",
                    value="https://example.com/recent",
                )
                first.last_success_at = now
                second = await SourcesRepo(session).create_connector(
                    channel_id=81,
                    kind="telegram",
                    value="@backlog_source",
                    config={TELEGRAM_BACKLOG_HINT_KEY: True},
                )
                second.last_success_at = now
                await SourcesRepo(session).create_connector(
                    channel_id=81,
                    kind="url",
                    value="https://example.com/never",
                )
                await session.commit()

            _RecordingService.calls = []
            monkeypatch.setattr(worker_module, "SourceIngestionService", _RecordingService)
            monkeypatch.setattr(
                worker_module,
                "TelegramSourceIngestionService",
                _RecordingService,
            )
            worker = SourceIngestionWorker(
                session_factory=Session,
                interval_seconds=15,
                max_connectors_per_tick=2,
            )

            assert await worker.run_once() == 2
            assert _RecordingService.calls == [2, 1]
            assert worker._last_connector_id == 2

            assert await worker.run_once() == 2
            # The second rotating window is [3, 1]. Never-successful #3 is due
            # before recently-successful #1, and rotation still wraps correctly.
            assert _RecordingService.calls == [2, 1, 3, 1]
            assert worker._last_connector_id == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_orders_oldest_success_before_newer_success(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            async with Session() as session:
                first = await SourcesRepo(session).create_connector(
                    channel_id=82,
                    kind="url",
                    value="https://example.com/newer",
                )
                first.last_success_at = now
                second = await SourcesRepo(session).create_connector(
                    channel_id=82,
                    kind="url",
                    value="https://example.com/older",
                )
                second.last_success_at = now - timedelta(hours=2)
                await session.commit()

            _RecordingService.calls = []
            monkeypatch.setattr(worker_module, "SourceIngestionService", _RecordingService)
            worker = SourceIngestionWorker(
                session_factory=Session,
                interval_seconds=15,
                max_connectors_per_tick=2,
            )
            assert await worker.run_once() == 2
            assert _RecordingService.calls == [2, 1]
        finally:
            await engine.dispose()

    asyncio.run(run())
