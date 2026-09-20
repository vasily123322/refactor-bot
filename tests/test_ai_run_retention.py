from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.ai_run_retention import AIRunRetentionService
from app.workers.ai_run_retention import AIRunRetentionWorker


async def _candidate(session, *, channel_id: int, status: str):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value=f"https://example.com/{channel_id}.xml",
    )
    document = await repo.add_document(
        SourceDocument(
            connector_id=int(connector.id),
            channel_id=channel_id,
            external_id=f"retention-{channel_id}",
            content="Retention source body",
            content_hash=f"{channel_id:064x}",
        )
    )
    candidate = await repo.add_candidate(
        ContentCandidate(
            source_document_id=int(document.id),
            channel_id=channel_id,
            suggested_action="research",
        )
    )
    candidate.status = status
    await session.commit()
    return candidate


def test_retention_preserves_active_running_recent_current_and_newest_rows() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        recent = now - timedelta(days=10)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                inactive = await _candidate(session, channel_id=801, status="accepted")
                active = await _candidate(session, channel_id=802, status="new")

                enrichment_rows = []
                for index in range(8):
                    row = CandidateEnrichmentRun(
                        candidate_id=inactive.id,
                        provider="fixture",
                        model="v1",
                        status="completed",
                        input_hash=f"{index:064x}",
                        input_chars=10,
                        output={},
                        created_at=old + timedelta(minutes=index),
                    )
                    session.add(row)
                    enrichment_rows.append(row)
                old_failed = CandidateEnrichmentRun(
                    candidate_id=inactive.id,
                    provider="fixture",
                    model="v1",
                    status="failed",
                    input_hash="f" * 64,
                    input_chars=10,
                    output={},
                    error="FixtureError",
                    created_at=old,
                )
                old_running = CandidateEnrichmentRun(
                    candidate_id=inactive.id,
                    provider="fixture",
                    model="v1",
                    status="running",
                    input_hash="e" * 64,
                    input_chars=10,
                    output={},
                    created_at=old,
                )
                recent_failed = CandidateEnrichmentRun(
                    candidate_id=inactive.id,
                    provider="fixture",
                    model="v1",
                    status="failed",
                    input_hash="d" * 64,
                    input_chars=10,
                    output={},
                    error="FixtureError",
                    created_at=recent,
                )
                active_old = CandidateEnrichmentRun(
                    candidate_id=active.id,
                    provider="fixture",
                    model="v1",
                    status="failed",
                    input_hash="c" * 64,
                    input_chars=10,
                    output={},
                    error="FixtureError",
                    created_at=old,
                )
                session.add_all([old_failed, old_running, recent_failed, active_old])

                rewrite_rows = []
                for index in range(8):
                    row = CandidateRewriteRun(
                        candidate_id=inactive.id,
                        provider="fixture",
                        model="v1",
                        status="completed",
                        input_hash=f"{100 + index:064x}",
                        input_chars=10,
                        text=f"rewrite {index}",
                        output={},
                        created_at=old + timedelta(minutes=index),
                    )
                    session.add(row)
                    rewrite_rows.append(row)
                rewrite_running = CandidateRewriteRun(
                    candidate_id=inactive.id,
                    provider="fixture",
                    model="v1",
                    status="running",
                    input_hash="b" * 64,
                    input_chars=10,
                    output={},
                    created_at=old,
                )
                session.add(rewrite_running)
                await session.commit()

                # Protect one old run that is not among the newest five.
                inactive.meta = {
                    **dict(inactive.meta or {}),
                    "enrichment_run_id": enrichment_rows[0].id,
                    "rewrite_run_id": rewrite_rows[0].id,
                }
                await session.commit()

                result = await AIRunRetentionService(session).cleanup(
                    retention_days=90,
                    keep_recent=5,
                    candidate_limit=100,
                    now=now,
                )
                assert result.candidates_scanned == 1
                assert result.enrichment_deleted == 3
                assert result.rewrite_deleted == 2

                enrichment_ids = set(
                    (
                        await session.execute(select(CandidateEnrichmentRun.id))
                    ).scalars().all()
                )
                rewrite_ids = set(
                    (await session.execute(select(CandidateRewriteRun.id))).scalars().all()
                )

                assert enrichment_rows[0].id in enrichment_ids
                assert all(row.id in enrichment_ids for row in enrichment_rows[-5:])
                assert old_running.id in enrichment_ids
                assert recent_failed.id in enrichment_ids
                assert active_old.id in enrichment_ids
                assert old_failed.id not in enrichment_ids

                assert rewrite_rows[0].id in rewrite_ids
                assert all(row.id in rewrite_ids for row in rewrite_rows[-5:])
                assert rewrite_running.id in rewrite_ids
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retention_is_bounded_by_candidate_limit_and_never_scans_active_candidates() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                first = await _candidate(session, channel_id=811, status="dismissed")
                second = await _candidate(session, channel_id=812, status="accepted")
                active = await _candidate(session, channel_id=813, status="new")
                for candidate in (first, second, active):
                    for index in range(7):
                        session.add(
                            CandidateEnrichmentRun(
                                candidate_id=candidate.id,
                                provider="fixture",
                                model="v1",
                                status="failed",
                                input_hash=f"{candidate.id * 100 + index:064x}",
                                input_chars=1,
                                output={},
                                error="FixtureError",
                                created_at=old,
                            )
                        )
                await session.commit()

                result = await AIRunRetentionService(session).cleanup(
                    retention_days=90,
                    keep_recent=5,
                    candidate_limit=1,
                    now=now,
                )
                assert result.candidates_scanned == 1
                assert result.enrichment_deleted == 2

                remaining_second = list(
                    (
                        await session.execute(
                            select(CandidateEnrichmentRun.id).where(
                                CandidateEnrichmentRun.candidate_id == second.id
                            )
                        )
                    ).scalars().all()
                )
                remaining_active = list(
                    (
                        await session.execute(
                            select(CandidateEnrichmentRun.id).where(
                                CandidateEnrichmentRun.candidate_id == active.id
                            )
                        )
                    ).scalars().all()
                )
                assert len(remaining_second) == 7
                assert len(remaining_active) == 7
        finally:
            await engine.dispose()

    asyncio.run(run())

def test_retention_worker_contains_tick_failure() -> None:
    async def run() -> None:
        worker = AIRunRetentionWorker(interval_seconds=60)
        calls = 0

        async def failing_tick() -> None:
            nonlocal calls
            calls += 1
            worker._stop_event.set()
            raise RuntimeError("fixture retention failure")

        worker._tick = failing_tick
        await worker._run()

        assert calls == 1

    asyncio.run(run())

