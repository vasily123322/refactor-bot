from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_drafts import CandidateDraftService
from app.services.candidate_rewrite import (
    CandidateRewriteError,
    CandidateRewriteService,
    RewriteInput,
    RewriteOutput,
)


class _RewriteProvider:
    name = "fixture"
    model = "rewrite-v1"

    def __init__(self, text: str = "Independent rewritten post.", mutate=None) -> None:
        self.text = text
        self.mutate = mutate
        self.calls = 0

    async def rewrite(self, payload: RewriteInput) -> RewriteOutput:
        self.calls += 1
        assert payload.reuse_policy == "rewrite_with_attribution"
        if self.mutate is not None:
            await self.mutate()
        return RewriteOutput(
            text=self.text,
            metadata={"generation_kind": "fixture"},
        )


async def _seed(session, *, channel_id: int, policy: str = "rewrite_with_attribution"):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value=f"https://example.com/{channel_id}.xml",
        reuse_policy=policy,
    )
    document, _ = await repo.upsert_document(
        connector=connector,
        external_id=f"entry-{channel_id}",
        title="Source title",
        content="SOURCE BODY MUST NOT BE COPIED VERBATIM.",
        source_url="https://example.com/article",
        metadata={"reuse_policy": policy},
    )
    candidate = await repo.ensure_candidate(
        source_document_id=document.id,
        channel_id=channel_id,
        suggested_action="rewrite",
        metadata={"reuse_policy": policy},
    )
    return connector, document, candidate


def test_completed_rewrite_is_reused_and_policy_safe_draft_consumes_it() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(session, channel_id=401)
                provider = _RewriteProvider("Independent editorial rewrite.")
                service = CandidateRewriteService(session)

                first = await service.rewrite(
                    channel_id=401,
                    candidate_id=candidate.id,
                    provider=provider,
                )
                second = await service.rewrite(
                    channel_id=401,
                    candidate_id=candidate.id,
                    provider=provider,
                )
                assert first.reused_existing is False
                assert second.reused_existing is True
                assert first.run.id == second.run.id
                assert provider.calls == 1
                assert first.run.status == "completed"
                assert first.run.text == "Independent editorial rewrite."

                draft = await CandidateDraftService(session).create(
                    channel_id=401,
                    candidate_id=candidate.id,
                )
                text = draft.document.primary_text()
                assert "Independent editorial rewrite." in text
                assert "https://example.com/article" in text
                assert "SOURCE BODY MUST NOT BE COPIED VERBATIM" not in text
                assert draft.document.metadata["rewrite_run_id"] == first.run.id
                assert draft.document.metadata["rewrite_provider"] == "fixture"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rewrite_requires_current_rewrite_with_attribution_policy() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(
                    session,
                    channel_id=402,
                    policy="reference_only",
                )
                with pytest.raises(CandidateRewriteError, match="does not allow"):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=402,
                        candidate_id=candidate.id,
                        provider=_RewriteProvider(),
                    )
                runs = (await session.execute(select(CandidateRewriteRun))).scalars().all()
                assert runs == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rewrite_is_stale_when_source_changes_during_provider_call() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(session, channel_id=403)

                async def mutate() -> None:
                    current = await session.get(SourceDocument, document.id)
                    assert current is not None
                    current.content = "Changed source snapshot."
                    current.content_hash = "changed-rewrite-hash"
                    await session.commit()

                with pytest.raises(CandidateRewriteError, match="source or policy changed"):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=403,
                        candidate_id=candidate.id,
                        provider=_RewriteProvider("Late rewrite", mutate=mutate),
                    )

                run_row = (await session.execute(select(CandidateRewriteRun))).scalar_one()
                candidate_row = await session.get(ContentCandidate, candidate.id)
                assert run_row.status == "stale"
                assert run_row.output["discard_reason"] == "source_or_policy_changed"
                assert run_row.text == "Late rewrite"
                assert candidate_row is not None
                assert "rewrite_run_id" not in dict(candidate_row.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rewrite_is_discarded_when_candidate_becomes_inactive() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, candidate = await _seed(session, channel_id=404)

                async def mutate() -> None:
                    current = await session.get(ContentCandidate, candidate.id)
                    assert current is not None
                    current.status = "dismissed"
                    await session.commit()

                with pytest.raises(CandidateRewriteError, match="no longer active"):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=404,
                        candidate_id=candidate.id,
                        provider=_RewriteProvider("Late rewrite", mutate=mutate),
                    )

                run_row = (await session.execute(select(CandidateRewriteRun))).scalar_one()
                assert run_row.status == "discarded"
                assert run_row.output["discard_reason"] == "candidate_not_active"
        finally:
            await engine.dispose()

    asyncio.run(run())
