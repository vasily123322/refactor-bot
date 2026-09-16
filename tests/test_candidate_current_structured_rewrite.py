from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentItem
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_current_structured_rewrite import (
    CandidateCurrentStructuredRewriteError,
    CandidateCurrentStructuredRewriteService,
)
from app.services.candidate_rewrite import candidate_rewrite_input_hash
from app.services.candidate_structured_rewrite_ai import STRUCTURED_REWRITE_GENERATION_KIND


def _post_document(text: str) -> dict:
    return {
        "schema_version": 1,
        "mode": "rich",
        "blocks": [{"id": "p", "type": "paragraph", "content": text}],
        "telegram": {},
        "metadata": {},
    }


async def _seed(session, *, channel_id: int):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value=f"https://example.com/{channel_id}.xml",
        reuse_policy="rewrite_with_attribution",
    )
    content = "Source body for structured rewrite authority."
    document = await repo.add_document(
        SourceDocument(
            connector_id=int(connector.id),
            channel_id=channel_id,
            external_id=f"entry-{channel_id}",
            title="Source title",
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_url="https://example.com/source",
            meta={"reuse_policy": "rewrite_with_attribution"},
        )
    )
    candidate = await repo.add_candidate(
        ContentCandidate(
            source_document_id=int(document.id),
            channel_id=channel_id,
            suggested_action="rewrite",
            meta={"reuse_policy": "rewrite_with_attribution"},
        )
    )
    await session.commit()
    await session.refresh(document)
    await session.refresh(candidate)
    return connector, document, candidate


async def _persist_run(
    session,
    *,
    candidate: ContentCandidate,
    document: SourceDocument,
    text: str,
    provider: str = "channel_ai_structured",
    status: str = "completed",
    model: str = "model-v1",
    output: dict | None = None,
    make_current: bool = True,
) -> CandidateRewriteRun:
    row = CandidateRewriteRun(
        candidate_id=int(candidate.id),
        provider=provider,
        model=model,
        status=status,
        input_hash=candidate_rewrite_input_hash(
            document,
            candidate,
            "rewrite_with_attribution",
        ),
        input_chars=len(document.content),
        text=text,
        output=(
            {
                "generation_kind": STRUCTURED_REWRITE_GENERATION_KIND,
                "post_document": _post_document(text),
            }
            if output is None
            else output
        ),
    )
    session.add(row)
    await session.flush()
    if make_current:
        candidate.meta = {
            **dict(candidate.meta or {}),
            "rewrite_run_id": int(row.id),
            "rewrite_provider": provider,
            "rewrite_model": model,
        }
    await session.commit()
    return row


def test_completed_structured_rewrite_survives_refresh_without_content_mutation() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(session, channel_id=811)
                persisted = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Recovered semantic body",
                )
                run_id = int(persisted.id)

            async with Session() as refreshed:
                current = await CandidateCurrentStructuredRewriteService(refreshed).current(
                    channel_id=811,
                    candidate_id=1,
                )
                assert current is not None
                assert int(current.run.id) == run_id
                assert current.document.to_dict()["blocks"] == _post_document(
                    "Recovered semantic body"
                )["blocks"]
                candidate = await refreshed.get(ContentCandidate, 1)
                assert candidate is not None
                assert candidate.content_item_id is None
                assert candidate.status == "new"
                assert (await refreshed.execute(select(ContentItem))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_changed_input_plain_failed_invalid_and_missing_runs_are_not_current() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(session, channel_id=812)
                service = CandidateCurrentStructuredRewriteService(session)

                completed = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Current",
                )
                assert await service.current(channel_id=812, candidate_id=candidate.id) is not None

                document.content_hash = "changed-input-hash"
                await session.commit()
                assert await service.current(channel_id=812, candidate_id=candidate.id) is None
                document.content_hash = completed.input_hash
                await session.commit()

                completed.input_hash = candidate_rewrite_input_hash(
                    document,
                    candidate,
                    "rewrite_with_attribution",
                )
                await session.commit()

                plain = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Plain",
                    provider="channel_ai",
                )
                assert int((candidate.meta or {})["rewrite_run_id"]) == int(plain.id)
                assert await service.current(channel_id=812, candidate_id=candidate.id) is None

                failed = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Failed",
                    status="failed",
                )
                assert int((candidate.meta or {})["rewrite_run_id"]) == int(failed.id)
                assert await service.current(channel_id=812, candidate_id=candidate.id) is None

                invalid = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Invalid",
                    output={
                        "generation_kind": STRUCTURED_REWRITE_GENERATION_KIND,
                        "post_document": {
                            "schema_version": 1,
                            "mode": "rich",
                            "blocks": [{"id": "bad", "type": "table", "rows": []}],
                            "telegram": {},
                            "metadata": {},
                        },
                    },
                )
                assert int((candidate.meta or {})["rewrite_run_id"]) == int(invalid.id)
                assert await service.current(channel_id=812, candidate_id=candidate.id) is None

                candidate.meta = {
                    **dict(candidate.meta or {}),
                    "rewrite_run_id": 999_999,
                    "rewrite_provider": "channel_ai_structured",
                }
                await session.commit()
                assert await service.current(channel_id=812, candidate_id=candidate.id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_current_pointer_is_deterministic_and_preserves_completed_history() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(session, channel_id=813)
                old = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Old proposal",
                )
                new = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="New proposal",
                    model="model-v2",
                )
                current = await CandidateCurrentStructuredRewriteService(session).current(
                    channel_id=813,
                    candidate_id=candidate.id,
                )
                assert current is not None
                assert int(current.run.id) == int(new.id)
                assert current.document.primary_text() == "New proposal"
                historical = await session.get(CandidateRewriteRun, int(old.id))
                assert historical is not None
                assert historical.status == "completed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_explicit_apply_after_refresh_uses_expected_current_run_and_stale_apply_fails() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, document, candidate = await _seed(session, channel_id=814)
                persisted = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Apply after refresh",
                )
                run_id = int(persisted.id)

            async with Session() as refreshed:
                result = await CandidateCurrentStructuredRewriteService(refreshed).apply(
                    channel_id=814,
                    candidate_id=1,
                    expected_run_id=run_id,
                )
                assert result.document.metadata["rewrite_run_id"] == run_id
                assert result.document.primary_text().startswith("Apply after refresh")

            async with Session() as session:
                _, document, candidate = await _seed(session, channel_id=815)
                persisted = await _persist_run(
                    session,
                    candidate=candidate,
                    document=document,
                    text="Will become stale",
                )
                stale_run_id = int(persisted.id)
                document.content = "Changed source body"
                document.content_hash = "changed-after-preview"
                await session.commit()

                with pytest.raises(
                    CandidateCurrentStructuredRewriteError,
                    match="no longer current",
                ):
                    await CandidateCurrentStructuredRewriteService(session).apply(
                        channel_id=815,
                        candidate_id=candidate.id,
                        expected_run_id=stale_run_id,
                    )
                candidate = await session.get(ContentCandidate, candidate.id)
                assert candidate is not None
                assert candidate.content_item_id is None
                stale_items = (
                    await session.execute(
                        select(ContentItem).where(ContentItem.channel_id == 815)
                    )
                ).scalars().all()
                assert stale_items == []
        finally:
            await engine.dispose()

    asyncio.run(run())
