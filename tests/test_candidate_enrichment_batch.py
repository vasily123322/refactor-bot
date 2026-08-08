from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_enrichment_batch import LocalBatchEnrichmentService


def test_local_batch_enriches_only_active_incomplete_candidates() -> None:
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
                    document, _ = await repo.upsert_document(
                        connector=connector,
                        external_id=f"entry-{index}",
                        title=f"Item {index}",
                        content=f"Sentence {index}. More useful context for item {index}.",
                        source_url=f"https://example.com/{index}",
                        metadata={"reuse_policy": "summarize"},
                    )
                    candidates.append(
                        await repo.ensure_candidate(
                            source_document_id=document.id,
                            channel_id=301,
                            suggested_action="summarize",
                            metadata={"reuse_policy": "summarize"},
                        )
                    )
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
                    document, _ = await repo.upsert_document(
                        connector=connector,
                        external_id=f"limited-{index}",
                        content=f"Body {index}. Second sentence.",
                    )
                    await repo.ensure_candidate(
                        source_document_id=document.id,
                        channel_id=302,
                        suggested_action="research",
                    )

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
