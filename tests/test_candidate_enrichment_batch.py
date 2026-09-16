from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_enrichment_batch import LocalBatchEnrichmentService


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _add_candidate(
    repo: SourcesRepo,
    *,
    connector_id: int,
    channel_id: int,
    external_id: str,
    content: str,
    suggested_action: str,
    title: str | None = None,
    source_url: str | None = None,
    metadata: dict | None = None,
) -> tuple[SourceDocument, ContentCandidate]:
    document = SourceDocument(
        connector_id=int(connector_id),
        channel_id=int(channel_id),
        external_id=external_id,
        title=title,
        content=content,
        content_hash=_content_hash(content),
        source_url=source_url,
        meta=dict(metadata or {}),
    )
    await repo.add_document(document)
    candidate = ContentCandidate(
        source_document_id=int(document.id),
        channel_id=int(channel_id),
        suggested_action=suggested_action,
        meta=dict(metadata or {}),
    )
    await repo.add_candidate(candidate)
    return document, candidate


def test_local_batch_enriches_only_active_untouched_candidates() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=301,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    reuse_policy="summarize",
                )
                candidates = []
                for index in range(3):
                    _, candidate = await _add_candidate(
                        repo,
                        connector_id=int(connector.id),
                        channel_id=301,
                        external_id=f"entry-{index}",
                        title=f"Item {index}",
                        content=f"Sentence {index}. More useful context for item {index}.",
                        source_url=f"https://example.com/{index}",
                        suggested_action="summarize",
                        metadata={"reuse_policy": "summarize"},
                    )
                    candidates.append(candidate)
                candidates[2].status = "dismissed"
                await session.commit()

                first = await LocalBatchEnrichmentService(session).run(
                    channel_id=301,
                    limit=100,
                )
                assert first.selected == 2
                assert first.completed == 2
                assert first.reused == 0
                assert first.skipped_busy == 0
                assert first.failed == 0

                for candidate in candidates[:2]:
                    await session.refresh(candidate)
                    assert candidate.summary
                    assert candidate.topic
                    assert candidate.score is not None
                await session.refresh(candidates[2])
                assert candidates[2].summary is None

                second = await LocalBatchEnrichmentService(session).run(
                    channel_id=301,
                    limit=100,
                )
                assert second.selected == 0
                assert second.completed == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_local_batch_never_overwrites_existing_summary_when_optional_score_is_null() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=303,
                    kind="rss",
                    value="https://example.com/303.xml",
                )
                _, candidate = await _add_candidate(
                    repo,
                    connector_id=int(connector.id),
                    channel_id=303,
                    external_id="pre-enriched",
                    title="Existing AI topic",
                    content="Original body that local enrichment must not replace.",
                    suggested_action="summarize",
                )
                candidate.summary = "Existing AI summary"
                candidate.topic = "Existing AI topic"
                candidate.score = None
                await session.commit()

                result = await LocalBatchEnrichmentService(session).run(
                    channel_id=303,
                    limit=100,
                )
                assert result.selected == 0
                await session.refresh(candidate)
                assert candidate.summary == "Existing AI summary"
                assert candidate.topic == "Existing AI topic"
                assert candidate.score is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_local_batch_respects_limit_and_channel_scope() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=302,
                    kind="rss",
                    value="https://example.com/302.xml",
                )
                for index in range(4):
                    await _add_candidate(
                        repo,
                        connector_id=int(connector.id),
                        channel_id=302,
                        external_id=f"limited-{index}",
                        content=f"Body {index}. Second sentence.",
                        suggested_action="research",
                    )
                await session.commit()

                result = await LocalBatchEnrichmentService(session).run(
                    channel_id=302,
                    limit=2,
                )
                assert result.selected == 2
                assert result.completed == 2

                foreign = await LocalBatchEnrichmentService(session).run(
                    channel_id=999,
                    limit=100,
                )
                assert foreign.selected == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
