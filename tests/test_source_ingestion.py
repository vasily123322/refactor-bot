from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import (
    MAX_SOURCE_DOCUMENT_CHARS,
    SourceIngestionError,
    SourceIngestionService,
)


def test_rss_ingestion_is_idempotent_and_creates_candidates() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            feed = """<?xml version="1.0"?>
            <rss version="2.0"><channel>
              <item>
                <guid>item-1</guid><title>First</title>
                <link>https://example.com/1</link>
                <description><![CDATA[<p>Hello <b>world</b></p>]]></description>
                <pubDate>Sat, 08 Aug 2026 08:00:00 GMT</pubDate>
              </item>
              <item>
                <guid>item-2</guid><title>Second</title>
                <link>https://example.com/2</link>
                <description>Body two</description>
              </item>
            </channel></rss>"""

            async def fetcher(url: str) -> str:
                assert url == "https://example.com/feed.xml"
                return feed

            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=41,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    mode="summary",
                )
                service = SourceIngestionService(session, fetcher=fetcher)
                first = await service.ingest(connector)
                second = await service.ingest(connector)

                assert first.documents_seen == 2
                assert first.documents_created == 2
                assert first.candidates_created == 2
                assert second.documents_seen == 2
                assert second.documents_created == 0
                assert second.candidates_created == 0

                documents = (
                    await session.execute(
                        select(SourceDocument).order_by(SourceDocument.external_id)
                    )
                ).scalars().all()
                candidates = (
                    await session.execute(
                        select(ContentCandidate).order_by(ContentCandidate.id)
                    )
                ).scalars().all()
                assert len(documents) == 2
                assert len(candidates) == 2
                assert documents[0].content == "First\nHello world"
                assert documents[0].source_url == "https://example.com/1"
                assert documents[0].published_at is not None
                assert all(row.suggested_action == "summarize" for row in candidates)
                assert connector.status == "healthy"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_web_ingestion_strips_nonvisible_content_and_caps_document_size() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            huge = "x" * (MAX_SOURCE_DOCUMENT_CHARS + 500)
            payload = f"<html><script>secret()</script><p>Visible</p><p>{huge}</p></html>"

            async def fetcher(_url: str) -> str:
                return payload

            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=42,
                    kind="url",
                    value="https://example.com/article",
                    mode="rewrite",
                    reuse_policy="rewrite_with_attribution",
                )
                result = await SourceIngestionService(session, fetcher=fetcher).ingest(
                    connector
                )
                assert result.documents_created == 1
                document = (
                    await session.execute(select(SourceDocument))
                ).scalar_one()
                assert "secret()" not in document.content
                assert document.content.startswith("Visible")
                assert len(document.content) <= MAX_SOURCE_DOCUMENT_CHARS + 1
                candidate = (
                    await session.execute(select(ContentCandidate))
                ).scalar_one()
                assert candidate.suggested_action == "rewrite"
                assert candidate.meta["reuse_policy"] == "rewrite_with_attribution"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("<rss><channel><item></channel>", "not valid XML"),
        (
            '<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>',
            "DTD and custom entities",
        ),
    ],
)
def test_rss_ingestion_fails_closed_and_records_health(payload: str, expected: str) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async def fetcher(_url: str) -> str:
                return payload

            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=43,
                    kind="rss",
                    value="https://example.com/feed.xml",
                )
                with pytest.raises(SourceIngestionError, match=expected):
                    await SourceIngestionService(session, fetcher=fetcher).ingest(connector)
                assert connector.status == "broken"
                assert expected in str(connector.status_reason)
                assert connector.last_error_at is not None
                assert (
                    await session.execute(select(SourceDocument))
                ).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())
