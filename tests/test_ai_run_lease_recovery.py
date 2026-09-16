from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.ai_run_leases import AI_RUN_LEASE_SECONDS, ai_run_lease_expired
from app.services.candidate_enrichment import (
    CandidateEnrichmentBusy,
    CandidateEnrichmentError,
    CandidateEnrichmentService,
    EnrichmentInput,
    EnrichmentOutput,
    candidate_enrichment_input_hash,
)
from app.services.candidate_rewrite import (
    CandidateRewriteBusy,
    CandidateRewriteError,
    CandidateRewriteService,
    RewriteInput,
    RewriteOutput,
    candidate_rewrite_input_hash,
)


class _Enricher:
    name = "lease-fixture"
    model = "v1"

    def __init__(self, *, mutate=None) -> None:
        self.mutate = mutate
        self.calls = 0

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput:
        self.calls += 1
        if self.mutate is not None:
            await self.mutate()
        return EnrichmentOutput(summary="Recovered summary", topic="Recovered", score=0.7)


class _Rewriter:
    name = "lease-fixture"
    model = "v1"

    def __init__(self, *, mutate=None) -> None:
        self.mutate = mutate
        self.calls = 0

    async def rewrite(self, payload: RewriteInput) -> RewriteOutput:
        self.calls += 1
        if self.mutate is not None:
            await self.mutate()
        return RewriteOutput(text="A fresh independent editorial version of the material.")


async def _seed(session, *, channel_id: int, policy: str):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value=f"https://example.com/{channel_id}.xml",
        reuse_policy=policy,
    )
    document = await repo.add_document(
        SourceDocument(
            connector_id=int(connector.id),
            channel_id=channel_id,
            external_id=f"lease-{channel_id}",
            title="Lease source",
            content="Original factual source material for lease recovery testing.",
            content_hash="e" * 64,
            source_url="https://example.com/article",
            meta={"reuse_policy": policy},
        )
    )
    candidate = await repo.add_candidate(
        ContentCandidate(
            source_document_id=int(document.id),
            channel_id=channel_id,
            suggested_action="rewrite" if policy == "rewrite_with_attribution" else "summarize",
            meta={"reuse_policy": policy},
        )
    )
    return connector, document, candidate


def test_lease_helper_handles_naive_and_missing_timestamps() -> None:
    now = datetime.now(timezone.utc)
    assert ai_run_lease_expired(None, now=now) is True
    assert ai_run_lease_expired(
        (now - timedelta(seconds=AI_RUN_LEASE_SECONDS + 1)).replace(tzinfo=None),
        now=now,
    ) is True
    assert ai_run_lease_expired(now - timedelta(seconds=10), now=now) is False


def test_expired_enrichment_run_is_abandoned_and_replaced() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(
                    session,
                    channel_id=701,
                    policy="summarize",
                )
                old = CandidateEnrichmentRun(
                    candidate_id=candidate.id,
                    provider="lease-fixture",
                    model="v1",
                    status="running",
                    input_hash=candidate_enrichment_input_hash(document, candidate),
                    input_chars=10,
                    output={},
                    started_at=datetime.now(timezone.utc) - timedelta(hours=1),
                )
                session.add(old)
                await session.commit()
                await session.refresh(old)

                result = await CandidateEnrichmentService(session).enrich(
                    channel_id=701,
                    candidate_id=candidate.id,
                    provider=_Enricher(),
                )
                await session.refresh(old)
                assert old.status == "abandoned"
                assert old.error == "LeaseExpired"
                assert old.output["recovery_reason"] == "lease_expired"
                assert old.finished_at is not None
                assert result.run.id != old.id
                assert result.run.status == "completed"
                assert result.candidate.summary == "Recovered summary"
                assert result.candidate.meta["enrichment_run_id"] == result.run.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fresh_enrichment_run_remains_busy() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(
                    session,
                    channel_id=702,
                    policy="summarize",
                )
                session.add(
                    CandidateEnrichmentRun(
                        candidate_id=candidate.id,
                        provider="lease-fixture",
                        model="v1",
                        status="running",
                        input_hash=candidate_enrichment_input_hash(document, candidate),
                        input_chars=10,
                        output={},
                        started_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                provider = _Enricher()
                with pytest.raises(CandidateEnrichmentBusy):
                    await CandidateEnrichmentService(session).enrich(
                        channel_id=702,
                        candidate_id=candidate.id,
                        provider=provider,
                    )
                assert provider.calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_late_enrichment_result_cannot_resurrect_abandoned_run() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(
                    session,
                    channel_id=703,
                    policy="summarize",
                )

                async def abandon_current() -> None:
                    current = (
                        await session.execute(
                            select(CandidateEnrichmentRun).where(
                                CandidateEnrichmentRun.candidate_id == candidate.id,
                                CandidateEnrichmentRun.status == "running",
                            )
                        )
                    ).scalar_one()
                    current.status = "abandoned"
                    current.error = "LeaseExpired"
                    current.output = {"recovery_reason": "lease_expired"}
                    current.finished_at = datetime.now(timezone.utc)
                    await session.commit()

                with pytest.raises(CandidateEnrichmentError, match="no longer active"):
                    await CandidateEnrichmentService(session).enrich(
                        channel_id=703,
                        candidate_id=candidate.id,
                        provider=_Enricher(mutate=abandon_current),
                    )
                run_row = (await session.execute(select(CandidateEnrichmentRun))).scalar_one()
                candidate_row = await session.get(ContentCandidate, candidate.id)
                assert run_row.status == "abandoned"
                assert run_row.error == "LeaseExpired"
                assert run_row.summary is None
                assert candidate_row is not None
                assert candidate_row.summary is None
                assert "enrichment_run_id" not in dict(candidate_row.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_rewrite_run_is_abandoned_and_replaced() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(
                    session,
                    channel_id=704,
                    policy="rewrite_with_attribution",
                )
                input_hash = candidate_rewrite_input_hash(
                    document,
                    candidate,
                    "rewrite_with_attribution",
                )
                old = CandidateRewriteRun(
                    candidate_id=candidate.id,
                    provider="lease-fixture",
                    model="v1",
                    status="running",
                    input_hash=input_hash,
                    input_chars=10,
                    output={},
                    started_at=datetime.now(timezone.utc) - timedelta(hours=1),
                )
                session.add(old)
                await session.commit()
                await session.refresh(old)

                result = await CandidateRewriteService(session).rewrite(
                    channel_id=704,
                    candidate_id=candidate.id,
                    provider=_Rewriter(),
                )
                await session.refresh(old)
                assert old.status == "abandoned"
                assert old.error == "LeaseExpired"
                assert old.output["recovery_reason"] == "lease_expired"
                assert result.run.id != old.id
                assert result.run.status == "completed"
                assert result.candidate.meta["rewrite_run_id"] == result.run.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fresh_rewrite_run_remains_busy_and_late_result_cannot_resurrect() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(
                    session,
                    channel_id=705,
                    policy="rewrite_with_attribution",
                )
                input_hash = candidate_rewrite_input_hash(
                    document,
                    candidate,
                    "rewrite_with_attribution",
                )
                fresh = CandidateRewriteRun(
                    candidate_id=candidate.id,
                    provider="lease-fixture",
                    model="v1",
                    status="running",
                    input_hash=input_hash,
                    input_chars=10,
                    output={},
                    started_at=datetime.now(timezone.utc),
                )
                session.add(fresh)
                await session.commit()
                provider = _Rewriter()
                with pytest.raises(CandidateRewriteBusy):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=705,
                        candidate_id=candidate.id,
                        provider=provider,
                    )
                assert provider.calls == 0

                fresh.status = "failed"
                fresh.error = "test-reset"
                fresh.finished_at = datetime.now(timezone.utc)
                await session.commit()

                async def abandon_current() -> None:
                    current = (
                        await session.execute(
                            select(CandidateRewriteRun).where(
                                CandidateRewriteRun.candidate_id == candidate.id,
                                CandidateRewriteRun.status == "running",
                            )
                        )
                    ).scalar_one()
                    current.status = "abandoned"
                    current.error = "LeaseExpired"
                    current.output = {"recovery_reason": "lease_expired"}
                    current.finished_at = datetime.now(timezone.utc)
                    await session.commit()

                with pytest.raises(CandidateRewriteError, match="no longer active"):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=705,
                        candidate_id=candidate.id,
                        provider=_Rewriter(mutate=abandon_current),
                    )
                latest = (
                    await session.execute(
                        select(CandidateRewriteRun)
                        .where(CandidateRewriteRun.status == "abandoned")
                        .order_by(CandidateRewriteRun.id.desc())
                    )
                ).scalars().first()
                candidate_row = await session.get(ContentCandidate, candidate.id)
                assert latest is not None
                assert latest.error == "LeaseExpired"
                assert latest.text is None
                assert candidate_row is not None
                assert "rewrite_run_id" not in dict(candidate_row.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())
