from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.content import MediaAssetsRepo
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_drafts import CandidateDraftError, CandidateDraftService


async def _seed(session, *, channel_id: int, policy: str, content: str, summary: str | None = None):
    repo = SourcesRepo(session)
    connector = await repo.create_connector(
        channel_id=channel_id,
        kind="rss",
        value="https://example.com/feed.xml",
        reuse_policy=policy,
    )
    document, _ = await repo.upsert_document(
        connector=connector,
        external_id=f"entry-{policy}",
        title="Source title",
        content=content,
        source_url="https://example.com/article",
        metadata={"reuse_policy": policy},
    )
    candidate = await repo.ensure_candidate(
        source_document_id=document.id,
        channel_id=channel_id,
        suggested_action="review",
        metadata={"reuse_policy": policy},
    )
    candidate.summary = summary
    await session.commit()
    return candidate


async def _link_media_asset(session, *, candidate: ContentCandidate, channel_id: int):
    asset = await MediaAssetsRepo(session).create(
        channel_id=channel_id,
        kind="photo",
        source="telegram_source",
        telegram_file_id="opaque-photo-file-id",
        mime_type="image/jpeg",
        width=1200,
        height=630,
    )
    document = await session.get(SourceDocument, int(candidate.source_document_id))
    assert document is not None
    document.meta = {
        **dict(document.meta or {}),
        "media_asset_id": int(asset.id),
    }
    await session.commit()
    return asset


def test_reference_only_candidate_draft_keeps_source_body_out_and_is_idempotent() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                candidate = await _seed(
                    session,
                    channel_id=71,
                    policy="reference_only",
                    content="DO NOT COPY THIS SOURCE BODY",
                )
                service = CandidateDraftService(session)
                first = await service.create(
                    channel_id=71,
                    candidate_id=candidate.id,
                    created_by_tg_user_id=1001,
                )
                second = await service.create(
                    channel_id=71,
                    candidate_id=candidate.id,
                    created_by_tg_user_id=1001,
                )

                assert first.reused_existing is False
                assert second.reused_existing is True
                assert first.item.id == second.item.id
                text = first.document.primary_text()
                assert "DO NOT COPY THIS SOURCE BODY" not in text
                assert "https://example.com/article" in text
                assert "reference" in text
                assert first.document.metadata["reuse_policy"] == "reference_only"

                refreshed = await session.get(ContentCandidate, candidate.id)
                assert refreshed is not None
                assert refreshed.status == "accepted"
                assert refreshed.content_item_id == first.item.id
                assert (
                    await session.execute(select(ContentItem))
                ).scalars().all().__len__() == 1
                assert (
                    await session.execute(select(ContentRevision))
                ).scalars().all().__len__() == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mirror_authorized_candidate_can_copy_bounded_source_body() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                body = "AUTHORIZED " + ("x" * 5000)
                candidate = await _seed(
                    session,
                    channel_id=72,
                    policy="mirror_authorized",
                    content=body,
                )
                result = await CandidateDraftService(session).create(
                    channel_id=72,
                    candidate_id=candidate.id,
                )
                text = result.document.primary_text()
                assert text.startswith("AUTHORIZED")
                assert len(text) <= 3900
                assert result.document.metadata["source_body_truncated"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_quote_policy_uses_bounded_quote_with_attribution() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                candidate = await _seed(
                    session,
                    channel_id=73,
                    policy="quote_with_attribution",
                    content="quoted " + ("q" * 1200),
                )
                result = await CandidateDraftService(session).create(
                    channel_id=73,
                    candidate_id=candidate.id,
                )
                text = result.document.primary_text()
                assert "quoted" in text
                assert "https://example.com/article" in text
                assert len(text) < 1000
                assert result.document.metadata["source_body_truncated"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_summarize_policy_uses_existing_summary_instead_of_source_body() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                candidate = await _seed(
                    session,
                    channel_id=74,
                    policy="summarize",
                    content="ORIGINAL BODY",
                    summary="Independent summary",
                )
                result = await CandidateDraftService(session).create(
                    channel_id=74,
                    candidate_id=candidate.id,
                )
                text = result.document.primary_text()
                assert "Independent summary" in text
                assert "ORIGINAL BODY" not in text
                assert "https://example.com/article" in text
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_candidate_draft_is_channel_scoped() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                candidate = await _seed(
                    session,
                    channel_id=75,
                    policy="reference_only",
                    content="body",
                )
                with pytest.raises(CandidateDraftError, match="candidate not found"):
                    await CandidateDraftService(session).create(
                        channel_id=999,
                        candidate_id=candidate.id,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mirror_authorized_draft_attaches_promoted_media_as_rich_block() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                candidate = await _seed(
                    session,
                    channel_id=76,
                    policy="mirror_authorized",
                    content="Authorized source caption",
                )
                asset = await _link_media_asset(
                    session,
                    candidate=candidate,
                    channel_id=76,
                )
                result = await CandidateDraftService(session).create(
                    channel_id=76,
                    candidate_id=int(candidate.id),
                )

                assert result.document.mode == "rich"
                assert [block["type"] for block in result.document.blocks] == [
                    "paragraph",
                    "media",
                ]
                assert result.document.blocks[1] == {
                    "id": "m1",
                    "type": "media",
                    "asset_id": int(asset.id),
                    "kind": "photo",
                    "caption": "",
                }
                assert result.document.metadata["source_media_asset_id"] == int(asset.id)
                assert result.document.metadata["source_media_attached"] is True
                assert "Authorized source caption" in result.document.primary_text()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reference_only_promoted_media_remains_provenance_not_publishable_block() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                candidate = await _seed(
                    session,
                    channel_id=77,
                    policy="reference_only",
                    content="SOURCE MEDIA BODY MUST NOT AUTO-PUBLISH",
                )
                asset = await _link_media_asset(
                    session,
                    candidate=candidate,
                    channel_id=77,
                )
                result = await CandidateDraftService(session).create(
                    channel_id=77,
                    candidate_id=int(candidate.id),
                )

                assert result.document.mode == "classic"
                assert [block["type"] for block in result.document.blocks] == ["text"]
                assert result.document.metadata["source_media_asset_id"] == int(asset.id)
                assert result.document.metadata["source_media_attached"] is False
                assert "SOURCE MEDIA BODY MUST NOT AUTO-PUBLISH" not in result.document.primary_text()
        finally:
            await engine.dispose()

    asyncio.run(run())
