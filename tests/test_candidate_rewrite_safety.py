from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_rewrite import (
    CandidateRewriteError,
    CandidateRewriteService,
    RewriteInput,
    RewriteOutput,
)


class _Provider:
    name = "unsafe-fixture"
    model = "v1"

    def __init__(self, text: str) -> None:
        self.text = text

    async def rewrite(self, payload: RewriteInput) -> RewriteOutput:
        return RewriteOutput(text=self.text)


async def _seed(session, *, channel_id: int, body: str):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value=f"https://example.com/{channel_id}.xml",
        reuse_policy="rewrite_with_attribution",
    )
    document, _ = await repo.upsert_document(
        connector=connector,
        external_id=f"unsafe-{channel_id}",
        title="Source",
        content=body,
        source_url="https://example.com/source-article",
        metadata={"reuse_policy": "rewrite_with_attribution"},
    )
    candidate = await repo.ensure_candidate(
        source_document_id=document.id,
        channel_id=channel_id,
        suggested_action="rewrite",
        metadata={"reuse_policy": "rewrite_with_attribution"},
    )
    return candidate


def test_rewrite_rejects_exact_source_copy_and_marks_run_failed() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                body = "This source body is intentionally long enough to be meaningful. " * 5
                candidate = await _seed(session, channel_id=411, body=body)
                with pytest.raises(CandidateRewriteError, match="too similar"):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=411,
                        candidate_id=candidate.id,
                        provider=_Provider(body),
                    )
                run_row = (await session.execute(select(CandidateRewriteRun))).scalar_one()
                assert run_row.status == "failed"
                assert run_row.text is None
                assert run_row.error == "CandidateRewriteError"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rewrite_rejects_near_verbatim_long_source_passage() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                body = " ".join(f"source-token-{index}" for index in range(90))
                candidate = await _seed(session, channel_id=412, body=body)
                generated = body + " A tiny closing sentence."
                with pytest.raises(CandidateRewriteError, match="verbatim|similar"):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=412,
                        candidate_id=candidate.id,
                        provider=_Provider(generated),
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rewrite_rejects_source_url_from_model_output() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                candidate = await _seed(
                    session,
                    channel_id=413,
                    body="A factual source about a product launch and a new distribution plan.",
                )
                generated = (
                    "The company introduced its product and outlined a new distribution plan. "
                    "Read more at https://example.com/source-article"
                )
                with pytest.raises(CandidateRewriteError, match="attribution"):
                    await CandidateRewriteService(session).rewrite(
                        channel_id=413,
                        candidate_id=candidate.id,
                        provider=_Provider(generated),
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())
