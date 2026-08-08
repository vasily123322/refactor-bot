from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_enrichment import (
    CandidateEnrichmentError,
    CandidateEnrichmentService,
    EnrichmentInput,
    EnrichmentOutput,
)


async def _seed(session, *, channel_id: int):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value="https://example.com/feed.xml",
        reuse_policy="summarize",
    )
    document, _ = await repo.upsert_document(
        connector=connector,
        external_id="entry-race",
        title="Race",
        content="Original source snapshot.",
        source_url="https://example.com/race",
        metadata={"reuse_policy": "summarize"},
    )
    candidate = await repo.ensure_candidate(
        source_document_id=document.id,
        channel_id=channel_id,
        suggested_action="summarize",
        metadata={"reuse_policy": "summarize"},
    )
    return document, candidate


class _MutatingProvider:
    name = "race"
    model = "v1"

    def __init__(self, mutate) -> None:
        self.mutate = mutate

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput:
        await self.mutate()
        return EnrichmentOutput(summary="Late summary", topic="Late", score=0.7)


def test_enrichment_result_is_marked_stale_when_source_changes_during_provider_call() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                document, candidate = await _seed(session, channel_id=111)

                async def mutate() -> None:
                    current = await session.get(SourceDocument, document.id)
                    assert current is not None
                    current.content = "New source snapshot while provider is running."
                    current.content_hash = "changed-hash"
                    await session.commit()

                with pytest.raises(CandidateEnrichmentError, match="source changed"):
                    await CandidateEnrichmentService(session).enrich(
                        channel_id=111,
                        candidate_id=candidate.id,
                        provider=_MutatingProvider(mutate),
                    )

                run_row = (
                    await session.execute(select(CandidateEnrichmentRun))
                ).scalar_one()
                candidate_row = await session.get(ContentCandidate, candidate.id)
                assert run_row.status == "stale"
                assert run_row.output["discard_reason"] == "source_snapshot_changed"
                assert run_row.summary == "Late summary"
                assert candidate_row is not None
                assert candidate_row.summary is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_enrichment_result_is_discarded_when_candidate_is_accepted_or_hidden() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, candidate = await _seed(session, channel_id=112)

                async def mutate() -> None:
                    current = await session.get(ContentCandidate, candidate.id)
                    assert current is not None
                    current.status = "dismissed"
                    await session.commit()

                with pytest.raises(CandidateEnrichmentError, match="no longer active"):
                    await CandidateEnrichmentService(session).enrich(
                        channel_id=112,
                        candidate_id=candidate.id,
                        provider=_MutatingProvider(mutate),
                    )

                run_row = (
                    await session.execute(select(CandidateEnrichmentRun))
                ).scalar_one()
                candidate_row = await session.get(ContentCandidate, candidate.id)
                assert run_row.status == "discarded"
                assert run_row.output["discard_reason"] == "candidate_not_active"
                assert candidate_row is not None
                assert candidate_row.status == "dismissed"
                assert candidate_row.summary is None
        finally:
            await engine.dispose()

    asyncio.run(run())
