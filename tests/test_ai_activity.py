from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.sources_v2 import SourcesRepo
from app.services.ai_activity import AIActivityService


def test_ai_activity_reports_usage_counts_and_safe_run_metadata() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                settings = await ChannelAISettingsRepo(session).get_or_create(601)
                settings.enabled = True
                settings.model = "provider/model"
                settings.temperature = 0.4
                settings.max_tokens = 900
                settings.tokens_used_day = 123
                settings.tokens_limit_day = 1000
                settings.tokens_used_month = 456
                settings.tokens_limit_month = 5000
                await session.commit()

                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=601,
                    kind="rss",
                    value="https://example.com/activity.xml",
                    reuse_policy="rewrite_with_attribution",
                )
                document = await repo.add_document(
                    SourceDocument(
                        connector_id=int(connector.id),
                        channel_id=601,
                        external_id="activity-entry",
                        title="Private source",
                        content="SECRET SOURCE BODY MUST NEVER ENTER AI ACTIVITY READ MODEL",
                        content_hash="d" * 64,
                        source_url="https://example.com/private",
                        meta={"reuse_policy": "rewrite_with_attribution"},
                    )
                )
                candidate = await repo.add_candidate(
                    ContentCandidate(
                        source_document_id=int(document.id),
                        channel_id=601,
                        suggested_action="rewrite",
                        meta={"reuse_policy": "rewrite_with_attribution"},
                    )
                )
                session.add_all(
                    [
                        CandidateEnrichmentRun(
                            candidate_id=candidate.id,
                            provider="local",
                            model="heuristic-v1",
                            status="completed",
                            input_hash="a" * 64,
                            input_chars=44,
                            summary="safe summary",
                            topic="topic",
                            score=0.7,
                            output={"score_kind": "local_content_quality"},
                        ),
                        CandidateEnrichmentRun(
                            candidate_id=candidate.id,
                            provider="channel_ai",
                            model="provider/model",
                            status="failed",
                            input_hash="b" * 64,
                            input_chars=55,
                            output={},
                            error="CandidateEnrichmentError",
                        ),
                        CandidateRewriteRun(
                            candidate_id=candidate.id,
                            provider="channel_ai",
                            model="provider/model",
                            status="completed",
                            input_hash="c" * 64,
                            input_chars=66,
                            text="generated rewrite text",
                            output={"generation_kind": "independent_rewrite_v1"},
                        ),
                    ]
                )
                await session.commit()

                snapshot = await AIActivityService(session).snapshot(
                    channel_id=601,
                    limit=20,
                )
                assert snapshot.usage.configured is True
                assert snapshot.usage.enabled is True
                assert snapshot.usage.model == "provider/model"
                assert snapshot.usage.tokens_used_day == 123
                assert snapshot.usage.tokens_limit_day == 1000
                assert snapshot.usage.tokens_used_month == 456
                assert snapshot.usage.tokens_limit_month == 5000
                assert snapshot.enrichment_counts == {"completed": 1, "failed": 1}
                assert snapshot.rewrite_counts == {"completed": 1}
                assert len(snapshot.runs) == 3
                assert {run.kind for run in snapshot.runs} == {"enrichment", "rewrite"}
                assert any(run.error_type == "CandidateEnrichmentError" for run in snapshot.runs)
                serialized = repr(snapshot)
                assert "SECRET SOURCE BODY" not in serialized
                assert "generated rewrite text" not in serialized
                assert "safe summary" not in serialized
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ai_activity_read_does_not_create_missing_ai_settings() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = ChannelAISettingsRepo(session)
                assert await repo.get_by_channel_id(602) is None
                snapshot = await AIActivityService(session).snapshot(channel_id=602)
                assert snapshot.usage.configured is False
                assert snapshot.usage.enabled is False
                assert snapshot.runs == ()
                assert await repo.get_by_channel_id(602) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
