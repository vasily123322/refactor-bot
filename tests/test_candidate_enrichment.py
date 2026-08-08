from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_enrichment import (
    CandidateEnrichmentError,
    CandidateEnrichmentService,
    EnrichmentInput,
    EnrichmentOutput,
    LocalCandidateEnricher,
)


async def _seed(session, *, channel_id: int = 101, content: str = "First sentence. Second sentence. Third sentence."):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value="https://example.com/feed.xml",
        reuse_policy="summarize",
    )
    document, _ = await repo.upsert_document(
        connector=connector,
        external_id="entry-1",
        title="Enrichment topic",
        content=content,
        source_url="https://example.com/article",
        metadata={"reuse_policy": "summarize"},
    )
    candidate = await repo.ensure_candidate(
        source_document_id=document.id,
        channel_id=channel_id,
        suggested_action="summarize",
        metadata={"reuse_policy": "summarize"},
    )
    return connector, document, candidate


def test_local_enrichment_updates_candidate_and_reuses_completed_run() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(session)
                service = CandidateEnrichmentService(session)
                first = await service.enrich(
                    channel_id=101,
                    candidate_id=candidate.id,
                    provider=LocalCandidateEnricher(),
                )
                second = await service.enrich(
                    channel_id=101,
                    candidate_id=candidate.id,
                    provider=LocalCandidateEnricher(),
                )

                assert first.reused_existing is False
                assert second.reused_existing is True
                assert second.run.id == first.run.id
                assert first.run.status == "completed"
                assert first.run.provider == "local"
                assert first.run.model == "heuristic-v1"
                assert first.candidate.summary == "First sentence. Second sentence."
                assert first.candidate.topic == "Enrichment topic"
                assert 0.0 <= float(first.candidate.score or 0) <= 1.0
                assert first.candidate.meta["enrichment_run_id"] == first.run.id
                assert first.run.output["score_kind"] == "local_content_quality"

                runs = (
                    await session.execute(select(CandidateEnrichmentRun))
                ).scalars().all()
                assert len(runs) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_source_snapshot_change_creates_a_new_enrichment_run() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector, document, candidate = await _seed(session, channel_id=102)
                service = CandidateEnrichmentService(session)
                first = await service.enrich(
                    channel_id=102,
                    candidate_id=candidate.id,
                    provider=LocalCandidateEnricher(),
                )
                await SourcesRepo(session).upsert_document(
                    connector=connector,
                    external_id=document.external_id,
                    title=document.title,
                    content="Updated source snapshot. Different second sentence.",
                    source_url=document.source_url,
                    metadata={"reuse_policy": "summarize"},
                )
                second = await service.enrich(
                    channel_id=102,
                    candidate_id=candidate.id,
                    provider=LocalCandidateEnricher(),
                )
                assert second.reused_existing is False
                assert second.run.id != first.run.id
                assert second.run.input_hash != first.run.input_hash
                assert "Updated source snapshot" in str(second.candidate.summary)
        finally:
            await engine.dispose()

    asyncio.run(run())


@dataclass
class _RecordingProvider:
    name: str = "recording"
    model: str = "v1"
    seen_chars: int = 0

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput:
        self.seen_chars = len(payload.text)
        return EnrichmentOutput(
            summary="Safe summary",
            topic="Safe topic",
            score=0.5,
            metadata={"provider_meta": "ok"},
        )


def test_enrichment_input_is_bounded_before_provider_call() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(
                    session,
                    channel_id=103,
                    content="x" * 50_000,
                )
                provider = _RecordingProvider()
                result = await CandidateEnrichmentService(session).enrich(
                    channel_id=103,
                    candidate_id=candidate.id,
                    provider=provider,
                )
                assert provider.seen_chars <= 12_000
                assert result.run.input_chars == provider.seen_chars
        finally:
            await engine.dispose()

    asyncio.run(run())


class _FailingProvider:
    name = "failing"
    model = "v1"

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput:
        raise RuntimeError("DO NOT STORE secret source body or provider credential")


def test_failed_enrichment_records_exception_type_without_sensitive_message() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(session, channel_id=104)
                with pytest.raises(CandidateEnrichmentError, match="enrichment failed"):
                    await CandidateEnrichmentService(session).enrich(
                        channel_id=104,
                        candidate_id=candidate.id,
                        provider=_FailingProvider(),
                    )
                run_row = (
                    await session.execute(select(CandidateEnrichmentRun))
                ).scalar_one()
                assert run_row.status == "failed"
                assert run_row.error == "RuntimeError"
                assert "secret" not in str(run_row.error).lower()
                assert run_row.finished_at is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


class _InvalidProvider:
    name = "invalid"
    model = "v1"

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput:
        return EnrichmentOutput(summary="ok", score=3.0)


def test_invalid_provider_output_fails_closed_and_is_channel_scoped() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(session, channel_id=105)
                service = CandidateEnrichmentService(session)
                with pytest.raises(CandidateEnrichmentError, match="between 0 and 1"):
                    await service.enrich(
                        channel_id=105,
                        candidate_id=candidate.id,
                        provider=_InvalidProvider(),
                    )
                with pytest.raises(CandidateEnrichmentError, match="candidate not found"):
                    await service.enrich(
                        channel_id=999,
                        candidate_id=candidate.id,
                        provider=LocalCandidateEnricher(),
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())
