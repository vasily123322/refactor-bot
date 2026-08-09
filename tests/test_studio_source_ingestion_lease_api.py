from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import ClassVar
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
from app.services.source_ingestion import IngestionResult, SourceIngestionError
from app.services.source_ingestion_lease import SourceIngestionLeaseService


class _RecordingSourceIngestionService:
    calls: ClassVar[list[int]] = []

    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        type(self).calls.append(int(connector.id))
        return IngestionResult(int(connector.id), 0, 0, 0)


class _DirtyFailingSourceIngestionService:
    def __init__(self, session) -> None:
        self.session = session

    async def ingest(self, connector):
        connector.config = {
            **dict(connector.config or {}),
            "partial_should_rollback": True,
        }
        raise SourceIngestionError("safe fixture failure")


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Lease",
                "username": f"lease_{user_id}",
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


def test_studio_manual_ingest_returns_conflict_while_connector_lease_is_busy(monkeypatch) -> None:
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
                "SourceIngestionService",
                _RecordingSourceIngestionService,
            )

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9131, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009131, "Lease")
                connector = await SourcesRepo(session).create_connector(
                    channel_id=channel.id,
                    kind="url",
                    value="https://example.com/lease",
                )
                connector_id = int(connector.id)
                worker_lease = await SourceIngestionLeaseService(session).acquire(
                    connector_id=connector_id,
                    holder="worker",
                )
                assert worker_lease is not None

            _RecordingSourceIngestionService.calls = []
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9131)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                blocked = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector_id}/ingest",
                    headers=headers,
                    json={},
                )
                assert blocked.status_code == 409
                assert blocked.json()["detail"] == "Source ingestion already running"
                assert _RecordingSourceIngestionService.calls == []

                async with Session() as session:
                    assert await SourceIngestionLeaseService(session).release(worker_lease) is True

                allowed = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector_id}/ingest",
                    headers=headers,
                    json={},
                )
                assert allowed.status_code == 200
                assert _RecordingSourceIngestionService.calls == [connector_id]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_failed_manual_ingest_rolls_back_dirty_state_before_separate_lease_release(monkeypatch) -> None:
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
                "SourceIngestionService",
                _DirtyFailingSourceIngestionService,
            )

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9132, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009132, "Rollback")
                connector = await SourcesRepo(session).create_connector(
                    channel_id=channel.id,
                    kind="url",
                    value="https://example.com/rollback",
                    config={"preserved": "yes"},
                )
                connector_id = int(connector.id)

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9132)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                response = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector_id}/ingest",
                    headers=headers,
                    json={},
                )
                assert response.status_code == 422

            async with Session() as session:
                connector = await SourcesRepo(session).get_connector(connector_id)
                assert connector is not None
                assert connector.config == {"preserved": "yes"}
                assert await SourceIngestionLeaseService(session).current(connector_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
