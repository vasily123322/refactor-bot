from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import SourceIngestionService
from app.services.source_reconciliation import (
    SourceIngestionReconciliationService,
    SourceProjection,
    SourceProjectionUpdateMode,
)


def test_reconciliation_is_idempotent_preserves_lifecycle_content_and_trusted_routing() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=71,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    mode="summary",
                    reuse_policy="reference_only",
                )
                service = SourceIngestionReconciliationService(session)
                first = await service.reconcile(
                    connector,
                    SourceProjection(
                        external_id="stable-1",
                        content="Original body",
                        source_url="https://example.com/original",
                        title="Original title",
                        metadata={"origin": "feed", "channel_id": 999},
                    ),
                )
                duplicate = await service.reconcile(
                    connector,
                    SourceProjection(
                        external_id="stable-1",
                        content="Original body",
                        source_url="https://example.com/original",
                        title="Original title",
                        metadata={"origin": "feed"},
                    ),
                )
                lifecycle = await service.reconcile(
                    connector,
                    SourceProjection(
                        external_id="stable-1",
                        metadata={"lifecycle": "approved"},
                        update_mode=SourceProjectionUpdateMode.LIFECYCLE,
                    ),
                )

                assert first.document_created is True
                assert first.candidate_created is True
                assert duplicate.document_created is False
                assert duplicate.candidate_created is False
                assert lifecycle.document_created is False
                assert lifecycle.candidate_created is False
                assert lifecycle.document.id == first.document.id
                assert lifecycle.candidate.id == first.candidate.id
                assert lifecycle.document.channel_id == 71
                assert lifecycle.candidate.channel_id == 71
                assert lifecycle.document.content == "Original body"
                assert lifecycle.document.source_url == "https://example.com/original"
                assert lifecycle.document.title == "Original title"
                assert lifecycle.document.meta["origin"] == "feed"
                assert lifecycle.document.meta["lifecycle"] == "approved"
                # Untrusted projection metadata may be retained as provenance, but it
                # never selects the canonical routing columns.
                assert lifecycle.document.meta["channel_id"] == 999
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reconciliation_does_not_reset_existing_candidate_lifecycle() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=72,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    mode="summary",
                )
                service = SourceIngestionReconciliationService(session)
                first = await service.reconcile(
                    connector,
                    SourceProjection(external_id="stable-2", content="Body"),
                )
                first.candidate.status = "approved"
                first.candidate.suggested_action = "manual"
                await session.commit()

                connector.mode = "rewrite"
                duplicate = await service.reconcile(
                    connector,
                    SourceProjection(external_id="stable-2", content="Body updated"),
                )
                assert duplicate.candidate.id == first.candidate.id
                assert duplicate.candidate.status == "approved"
                assert duplicate.candidate.suggested_action == "manual"
                assert duplicate.document.content == "Body updated"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reconciliation_rolls_back_document_if_candidate_stage_fails() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=73,
                    kind="rss",
                    value="https://example.com/feed.xml",
                )
                service = SourceIngestionReconciliationService(session)

                async def fail_candidate(_row: ContentCandidate) -> ContentCandidate:
                    raise RuntimeError("candidate persistence interrupted")

                service.repo.add_candidate = fail_candidate  # type: ignore[method-assign]
                with pytest.raises(RuntimeError, match="candidate persistence interrupted"):
                    await service.reconcile(
                        connector,
                        SourceProjection(external_id="atomic-1", content="Body"),
                    )

                assert (await session.execute(select(SourceDocument))).scalars().all() == []
                assert (await session.execute(select(ContentCandidate))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_document_unique_conflict_reloads_winner_and_reconciles_projection() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=74,
                    kind="rss",
                    value="https://example.com/feed.xml",
                )
                seed = SourceIngestionReconciliationService(session)
                winner = await seed.reconcile(
                    connector,
                    SourceProjection(external_id="race-doc", content="Winner body"),
                )

                service = SourceIngestionReconciliationService(session)
                real_lookup = service.repo.get_document_by_identity
                lookups = 0

                async def race_lookup(*, connector_id: int, external_id: str):
                    nonlocal lookups
                    lookups += 1
                    if lookups == 1:
                        return None
                    return await real_lookup(
                        connector_id=connector_id,
                        external_id=external_id,
                    )

                async def unique_conflict(_row: SourceDocument) -> SourceDocument:
                    raise IntegrityError("insert", {}, RuntimeError("unique"))

                service.repo.get_document_by_identity = race_lookup  # type: ignore[method-assign]
                service.repo.add_document = unique_conflict  # type: ignore[method-assign]
                reconciled = await service.reconcile(
                    connector,
                    SourceProjection(external_id="race-doc", content="Loser projection"),
                )

                assert reconciled.document_created is False
                assert reconciled.document.id == winner.document.id
                assert reconciled.document.content == "Loser projection"
                assert reconciled.candidate.id == winner.candidate.id
                assert len((await session.execute(select(SourceDocument))).scalars().all()) == 1
                assert len((await session.execute(select(ContentCandidate))).scalars().all()) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_candidate_unique_conflict_reloads_winner_without_resetting_it() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=75,
                    kind="rss",
                    value="https://example.com/feed.xml",
                )
                seed = SourceIngestionReconciliationService(session)
                winner = await seed.reconcile(
                    connector,
                    SourceProjection(external_id="race-candidate", content="Body"),
                )
                winner.candidate.status = "approved"
                await session.commit()

                service = SourceIngestionReconciliationService(session)
                real_lookup = service.repo.get_candidate_by_source_channel
                lookups = 0

                async def race_lookup(*, source_document_id: int, channel_id: int):
                    nonlocal lookups
                    lookups += 1
                    if lookups == 1:
                        return None
                    return await real_lookup(
                        source_document_id=source_document_id,
                        channel_id=channel_id,
                    )

                async def unique_conflict(_row: ContentCandidate) -> ContentCandidate:
                    raise IntegrityError("insert", {}, RuntimeError("unique"))

                service.repo.get_candidate_by_source_channel = race_lookup  # type: ignore[method-assign]
                service.repo.add_candidate = unique_conflict  # type: ignore[method-assign]
                reconciled = await service.reconcile(
                    connector,
                    SourceProjection(external_id="race-candidate", content="Body updated"),
                )

                assert reconciled.candidate_created is False
                assert reconciled.candidate.id == winner.candidate.id
                assert reconciled.candidate.status == "approved"
                assert len((await session.execute(select(ContentCandidate))).scalars().all()) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_generic_ingestion_repairs_missing_candidate_for_existing_document() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            feed = """<rss><channel><item><guid>repair-1</guid>
            <title>Repair</title><description>Body</description></item></channel></rss>"""

            async def fetcher(_url: str) -> str:
                return feed

            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=76,
                    kind="rss",
                    value="https://example.com/feed.xml",
                )
                service = SourceIngestionService(session, fetcher=fetcher)
                first = await service.ingest(connector)
                document = (await session.execute(select(SourceDocument))).scalar_one()
                await session.execute(delete(ContentCandidate))
                await session.commit()

                repaired = await service.ingest(connector)
                candidate = (await session.execute(select(ContentCandidate))).scalar_one()
                assert first.documents_created == 1
                assert first.candidates_created == 1
                assert repaired.documents_created == 0
                assert repaired.candidates_created == 1
                assert candidate.source_document_id == document.id
                assert candidate.channel_id == connector.channel_id
        finally:
            await engine.dispose()

    asyncio.run(run())
