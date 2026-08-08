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
from app.domain.models import AISource
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.sources_v2 import SourcesRepo


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Source",
                "username": f"source_{user_id}",
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


def test_source_settings_api_updates_owner_source_and_legacy_projection(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(sources_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9101, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009101, "Sources")
                other = await ClientsRepo(session).create_or_get(9102, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009102, "Foreign")
                legacy = AISource(
                    channel_id=channel.id,
                    source_type="rss",
                    source_value="https://example.com/feed.xml",
                    mode="summary",
                    enabled=True,
                    citation_enabled=True,
                )
                session.add(legacy)
                await session.commit()
                await session.refresh(legacy)
                connector = await SourcesRepo(session).create_connector(
                    channel_id=channel.id,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    legacy_ai_source_id=legacy.id,
                )

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9101)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                response = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{connector.id}/settings",
                    headers=headers,
                    json={
                        "enabled": False,
                        "mode": "rewrite",
                        "citation_enabled": False,
                        "reuse_policy": "rewrite_with_attribution",
                    },
                )
                assert response.status_code == 200
                body = response.json()
                assert body["enabled"] is False
                assert body["mode"] == "rewrite"
                assert body["citation_enabled"] is False
                assert body["reuse_policy"] == "rewrite_with_attribution"

                foreign_response = await client.post(
                    f"/api/studio/channels/{foreign.id}/sources/{connector.id}/settings",
                    headers=headers,
                    json={"enabled": True},
                )
                assert foreign_response.status_code == 404

            async with Session() as session:
                legacy_after = await session.get(AISource, legacy.id)
                assert legacy_after is not None
                assert legacy_after.enabled is False
                assert legacy_after.mode == "rewrite"
                assert legacy_after.citation_enabled is False
        finally:
            await engine.dispose()

    asyncio.run(run())
