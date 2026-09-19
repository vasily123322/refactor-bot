from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.app as studio_app_module
import app.api.studio.sources as sources_api_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_ingestion import IngestionResult


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Inbox",
                "username": f"inbox_{user_id}",
            },
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _config() -> StudioConfig:
    return StudioConfig(
        enabled=True,
        host="127.0.0.1",
        port=8080,
        public_url=None,
        init_data_max_age_seconds=86400,
        cors_origins=(),
    )


class _FakeIngestion:
    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector) -> IngestionResult:
        return IngestionResult(
            connector_id=int(connector.id),
            documents_seen=3,
            documents_created=2,
            candidates_created=2,
        )


def test_sources_inbox_is_owner_scoped_and_candidates_can_be_dismissed_and_restored(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(sources_api_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(sources_api_module, "SourceIngestionService", _FakeIngestion)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(5101, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1005101, "Inbox channel")
                other = await ClientsRepo(session).create_or_get(5102, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1005102, "Foreign")

                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=channel.id,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    mode="summary",
                    reuse_policy="summarize",
                )
                document, _ = await repo.upsert_document(
                    connector=connector,
                    external_id="entry-1",
                    title="Candidate title",
                    content="Candidate body that should appear only as a bounded excerpt.",
                    source_url="https://example.com/entry-1",
                    metadata={"reuse_policy": "summarize"},
                )
                candidate = await repo.ensure_candidate(
                    source_document_id=document.id,
                    channel_id=channel.id,
                    suggested_action="summarize",
                    metadata={"reuse_policy": "summarize"},
                )

            app = create_studio_app(_config())
            headers = {"X-Telegram-Init-Data": _init_data(5101)}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                listed = await client.get(
                    f"/api/studio/channels/{channel.id}/candidates",
                    headers=headers,
                )
                assert listed.status_code == 200
                body = listed.json()
                assert len(body) == 1
                assert body[0]["id"] == candidate.id
                assert body[0]["source_title"] == "Candidate title"
                assert body[0]["source_url"] == "https://example.com/entry-1"
                assert body[0]["reuse_policy"] == "summarize"
                assert "Candidate body" in body[0]["excerpt"]

                foreign_result = await client.get(
                    f"/api/studio/channels/{foreign.id}/candidates",
                    headers=headers,
                )
                assert foreign_result.status_code == 404

                ingested = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector.id}/ingest",
                    headers=headers,
                    json={},
                )
                assert ingested.status_code == 200
                assert ingested.json()["documents_created"] == 2

                dismissed = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/dismiss",
                    headers=headers,
                    json={},
                )
                assert dismissed.status_code == 200
                assert dismissed.json()["status"] == "dismissed"

                empty = await client.get(
                    f"/api/studio/channels/{channel.id}/candidates",
                    headers=headers,
                )
                assert empty.status_code == 200
                assert empty.json() == []

                dismissed_rows = await client.get(
                    f"/api/studio/channels/{channel.id}/candidates",
                    headers=headers,
                    params={"status_filter": "dismissed"},
                )
                assert dismissed_rows.status_code == 200
                assert [row["id"] for row in dismissed_rows.json()] == [candidate.id]

                foreign_restore = await client.post(
                    f"/api/studio/channels/{foreign.id}/candidates/{candidate.id}/restore",
                    headers=headers,
                    json={},
                )
                assert foreign_restore.status_code == 404

                restored = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/restore",
                    headers=headers,
                    json={},
                )
                assert restored.status_code == 200
                assert restored.json()["status"] == "new"

                restored_rows = await client.get(
                    f"/api/studio/channels/{channel.id}/candidates",
                    headers=headers,
                )
                assert restored_rows.status_code == 200
                assert [row["id"] for row in restored_rows.json()] == [candidate.id]

                dismissed_empty = await client.get(
                    f"/api/studio/channels/{channel.id}/candidates",
                    headers=headers,
                    params={"status_filter": "dismissed"},
                )
                assert dismissed_empty.status_code == 200
                assert dismissed_empty.json() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_manual_ingest_routes_telegram_through_mtproto_adapter(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(sources_api_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(
                sources_api_module,
                "TelegramSourceIngestionService",
                _FakeIngestion,
            )

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(5201, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1005201, "Telegram source")
                connector = await SourcesRepo(session).create_connector(
                    channel_id=channel.id,
                    kind="telegram",
                    value="@example",
                )

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(5201)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                response = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector.id}/ingest",
                    headers=headers,
                    json={},
                )
                assert response.status_code == 200
                assert response.json() == {
                    "connector_id": connector.id,
                    "documents_seen": 3,
                    "documents_created": 2,
                    "candidates_created": 2,
                }
        finally:
            await engine.dispose()

    asyncio.run(run())
