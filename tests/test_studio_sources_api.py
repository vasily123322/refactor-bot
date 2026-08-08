from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
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


def test_studio_sources_are_owner_scoped_and_keep_legacy_ai_flow_alive(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(sources_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(4001, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1004001, "Owner sources")
                other = await ClientsRepo(session).create_or_get(4002, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1004002, "Foreign")
                session.add(
                    AISource(
                        channel_id=channel.id,
                        source_type="rss",
                        source_value="https://example.com/original.xml",
                        mode="summary",
                        enabled=True,
                        citation_enabled=True,
                    )
                )
                await session.commit()

            app = create_studio_app(_config())
            headers = {"X-Telegram-Init-Data": _init_data(4001)}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                listed = await client.get(
                    f"/api/studio/channels/{channel.id}/sources",
                    headers=headers,
                )
                assert listed.status_code == 200
                assert len(listed.json()) == 1
                assert listed.json()[0]["reuse_policy"] == "reference_only"
                assert listed.json()[0]["legacy_ai_source_id"] is not None

                foreign_result = await client.get(
                    f"/api/studio/channels/{foreign.id}/sources",
                    headers=headers,
                )
                assert foreign_result.status_code == 404

                created = await client.post(
                    f"/api/studio/channels/{channel.id}/sources",
                    headers=headers,
                    json={
                        "kind": "url",
                        "value": "https://example.com/article",
                        "mode": "rewrite",
                        "citation_enabled": True,
                        "reuse_policy": "rewrite_with_attribution",
                    },
                )
                assert created.status_code == 201
                body = created.json()
                assert body["kind"] == "url"
                assert body["mode"] == "rewrite"
                assert body["reuse_policy"] == "rewrite_with_attribution"
                assert body["legacy_ai_source_id"] is not None

            async with Session() as session:
                rows = list(
                    (
                        await session.execute(
                            select(AISource).where(AISource.channel_id == channel.id)
                        )
                    ).scalars().all()
                )
                assert {row.source_value for row in rows} == {
                    "https://example.com/original.xml",
                    "https://example.com/article",
                }
                created_legacy = next(
                    row for row in rows if row.source_value.endswith("/article")
                )
                assert created_legacy.mode == "rewrite"
        finally:
            await engine.dispose()

    asyncio.run(run())
