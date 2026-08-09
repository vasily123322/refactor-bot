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
from app.services.source_ingestion_lease import SourceIngestionLeaseService


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Lifecycle",
                "username": f"lifecycle_{user_id}",
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


def test_studio_settings_return_conflict_while_ingestion_lease_is_active(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(sources_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9151, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009151, "Lifecycle")
                connector = await SourcesRepo(session).create_connector(
                    channel_id=channel.id,
                    kind="url",
                    value="https://example.com/lifecycle",
                    mode="summary",
                    reuse_policy="reference_only",
                )
                connector_id = int(connector.id)
                lease = await SourceIngestionLeaseService(session).acquire(
                    connector_id=connector_id,
                    holder="worker",
                )
                assert lease is not None

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9151)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                blocked = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector_id}/settings",
                    headers=headers,
                    json={"mode": "rewrite", "reuse_policy": "summarize"},
                )
                assert blocked.status_code == 409
                assert blocked.json()["detail"] == "Source ingestion is running"

                async with Session() as session:
                    row = await SourcesRepo(session).get_connector(connector_id)
                    assert row is not None
                    assert row.mode == "summary"
                    assert row.reuse_policy == "reference_only"
                    assert await SourceIngestionLeaseService(session).release(lease) is True

                allowed = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector_id}/settings",
                    headers=headers,
                    json={"mode": "rewrite", "reuse_policy": "summarize"},
                )
                assert allowed.status_code == 200
                assert allowed.json()["mode"] == "rewrite"
                assert allowed.json()["reuse_policy"] == "summarize"
        finally:
            await engine.dispose()

    asyncio.run(run())
