from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate
from app.repositories.sources_v2 import SourcesRepo
from app.workers.candidate_enrichment import LocalCandidateEnrichmentWorker


async def _candidate(
    session,
    *,
    channel_id: int,
    external_id: str,
    content: str,
    summary: str | None = None,
    status: str = "new",
) -> ContentCandidate:
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="url",
        value=f"https://example.test/{external_id}",
        reuse_policy="reference_only",
    )
    document, _ = await repo.upsert_document(
        connector=connector,
        external_id=external_id,
        content=content,
        source_url=f"https://example.test/article/{external_id}",
        metadata={},
    )
    candidate = await repo.ensure_candidate(
        source_document_id=document.id,
        channel_id=channel_id,
        suggested_action="research",
    )
    candidate.summary = summary
    candidate.status = status
    await session.commit()
    await session.refresh(candidate)
    return candidate


def test_local_enrichment_worker_is_bounded_and_never_overwrites_existing_enrichment() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                first = await _candidate(
                    session,
                    channel_id=81,
                    external_id="first",
                    content="Alpha launch happened today. Teams reported strong early engagement.",
                )
                second = await _candidate(
                    session,
                    channel_id=81,
                    external_id="second",
                    content="Beta update shipped later. Customers highlighted faster workflows.",
                )
                manual = await _candidate(
                    session,
                    channel_id=81,
                    external_id="manual",
                    content="This candidate already has editorial enrichment.",
                    summary="Manual summary must remain unchanged",
                )
                accepted = await _candidate(
                    session,
                    channel_id=81,
                    external_id="accepted",
                    content="Accepted candidates are outside the worker queue.",
                    status="accepted",
                )
                ids = {
                    "first": int(first.id),
                    "second": int(second.id),
                    "manual": int(manual.id),
                    "accepted": int(accepted.id),
                }

            worker = LocalCandidateEnrichmentWorker(
                session_factory=Session,
                batch_size=1,
                candidate_timeout_seconds=5,
            )
            first_tick = await worker.run_once()
            assert first_tick.selected == 1
            assert first_tick.completed == 1
            assert first_tick.failures == 0

            second_tick = await worker.run_once()
            assert second_tick.selected == 1
            assert second_tick.completed == 1

            third_tick = await worker.run_once()
            assert third_tick.selected == 0

            async with Session() as session:
                rows = list(
                    (
                        await session.execute(
                            select(ContentCandidate).order_by(ContentCandidate.id.asc())
                        )
                    ).scalars().all()
                )
                by_id = {int(row.id): row for row in rows}
                assert by_id[ids["first"]].summary
                assert by_id[ids["first"]].topic
                assert by_id[ids["first"]].score is not None
                assert by_id[ids["second"]].summary
                assert by_id[ids["manual"]].summary == "Manual summary must remain unchanged"
                assert by_id[ids["manual"]].topic is None
                assert by_id[ids["accepted"]].summary is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_local_enrichment_worker_isolates_candidate_failures(monkeypatch) -> None:
    async def run() -> None:
        worker = LocalCandidateEnrichmentWorker(batch_size=2)
        monkeypatch.setattr(worker, "_candidate_keys", lambda: _async_value([(1, 10), (1, 11)]))
        calls: list[int] = []

        async def enrich_one(channel_id: int, candidate_id: int):
            calls.append(candidate_id)
            if candidate_id == 10:
                raise RuntimeError("broken candidate")
            return SimpleNamespace(reused_existing=False)

        monkeypatch.setattr(worker, "_enrich_one", enrich_one)
        tick = await worker.run_once()
        assert calls == [10, 11]
        assert tick.selected == 2
        assert tick.completed == 1
        assert tick.failures == 1

    asyncio.run(run())


def test_local_enrichment_worker_start_stop_is_idempotent(monkeypatch) -> None:
    async def run() -> None:
        worker = LocalCandidateEnrichmentWorker(interval_seconds=60)
        started = asyncio.Event()

        async def run_once():
            started.set()
            return SimpleNamespace(
                selected=0,
                completed=0,
                reused=0,
                skipped_busy=0,
                failures=0,
                timeouts=0,
            )

        monkeypatch.setattr(worker, "run_once", run_once)
        await worker.start()
        first_task = worker._task
        await worker.start()
        assert worker._task is first_task
        await asyncio.wait_for(started.wait(), timeout=1)
        await worker.stop()
        assert worker._task is None
        await worker.stop()

    asyncio.run(run())


async def _async_value(value):
    return value
