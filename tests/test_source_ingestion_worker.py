from __future__ import annotations

import asyncio
from typing import ClassVar

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.source_ingestion as worker_module
from app.core.db import Base
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import IngestionResult, SourceIngestionError
from app.workers.source_ingestion import SourceIngestionWorker


class _FakeIngestionService:
    calls: ClassVar[list[int]] = []

    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        type(self).calls.append(int(connector.id))
        if len(type(self).calls) == 1:
            raise SourceIngestionError("first connector fails")
        return IngestionResult(int(connector.id), 1, 1, 1)


class _FakeTelegramIngestionService:
    calls: ClassVar[list[int]] = []

    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        type(self).calls.append(int(connector.id))
        return IngestionResult(int(connector.id), 1, 1, 1)


class _RecordingIngestionService:
    calls: ClassVar[list[int]] = []

    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        type(self).calls.append(int(connector.id))
        return IngestionResult(int(connector.id), 0, 0, 0)


def test_worker_isolates_connector_failures_and_dispatches_telegram(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await SourcesRepo(session).create_connector(
                    channel_id=51,
                    kind="rss",
                    value="https://example.com/one.xml",
                )
                await SourcesRepo(session).create_connector(
                    channel_id=51,
                    kind="url",
                    value="https://example.com/two",
                )
                await SourcesRepo(session).create_connector(
                    channel_id=51,
                    kind="telegram",
                    value="@telegram-worker",
                )

            _FakeIngestionService.calls = []
            _FakeTelegramIngestionService.calls = []
            monkeypatch.setattr(worker_module, "SourceIngestionService", _FakeIngestionService)
            monkeypatch.setattr(
                worker_module,
                "TelegramSourceIngestionService",
                _FakeTelegramIngestionService,
            )
            worker = SourceIngestionWorker(session_factory=Session, interval_seconds=15)
            processed = await worker.run_once()

            assert processed == 2
            assert len(_FakeIngestionService.calls) == 2
            assert len(_FakeTelegramIngestionService.calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_rotates_bounded_connector_window_without_starvation(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                for index in range(5):
                    await SourcesRepo(session).create_connector(
                        channel_id=52,
                        kind="url",
                        value=f"https://example.com/{index}",
                    )

            _RecordingIngestionService.calls = []
            monkeypatch.setattr(
                worker_module,
                "SourceIngestionService",
                _RecordingIngestionService,
            )
            worker = SourceIngestionWorker(
                session_factory=Session,
                interval_seconds=15,
                max_connectors_per_tick=2,
            )

            assert await worker.run_once() == 2
            assert await worker.run_once() == 2
            assert await worker.run_once() == 2
            assert _RecordingIngestionService.calls == [1, 2, 3, 4, 5, 1]
            assert worker._last_connector_id == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_start_stop_is_idempotent() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            worker = SourceIngestionWorker(session_factory=Session, interval_seconds=15)
            calls = 0
            entered = asyncio.Event()

            async def fake_run_once() -> int:
                nonlocal calls
                calls += 1
                entered.set()
                return 0

            worker.run_once = fake_run_once  # type: ignore[method-assign]
            await worker.start()
            first_task = worker._task
            await worker.start()
            assert worker._task is first_task
            await asyncio.wait_for(entered.wait(), timeout=1)
            await worker.stop()
            await worker.stop()
            assert calls >= 1
            assert worker._task is None
        finally:
            await engine.dispose()

    asyncio.run(run())
