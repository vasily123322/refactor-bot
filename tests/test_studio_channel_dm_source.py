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
from app.domain.sources.models import SourceConnector
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
                "first_name": "DM Source",
                "username": f"dm_source_{user_id}",
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


def test_channel_dm_connector_is_canonical_only_and_telegram_history_stays_legacy_backed(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(sources_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(4101, "dm_owner", "DM Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1004101, "DM channel")
                await session.commit()

            app = create_studio_app(_config())
            headers = {"X-Telegram-Init-Data": _init_data(4101)}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                dm_created = await client.post(
                    f"/api/studio/channels/{channel.id}/sources",
                    headers=headers,
                    json={
                        "kind": "telegram_channel_dms",
                        "value": str(channel.tg_chat_id),
                        "mode": "rewrite",
                        "citation_enabled": True,
                        "reuse_policy": "rewrite_with_attribution",
                    },
                )
                assert dm_created.status_code == 201
                dm_body = dm_created.json()
                assert dm_body["kind"] == "telegram_channel_dms"
                assert dm_body["channel_id"] == channel.id
                assert dm_body["legacy_ai_source_id"] is None
                assert dm_body["capabilities"]["live"] is True
                assert dm_body["capabilities"]["auth"] == "bot_api"

                telegram_created = await client.post(
                    f"/api/studio/channels/{channel.id}/sources",
                    headers=headers,
                    json={
                        "kind": "telegram",
                        "value": "legacy-history-chat",
                        "mode": "summary",
                    },
                )
                assert telegram_created.status_code == 201
                assert telegram_created.json()["legacy_ai_source_id"] is not None
                assert telegram_created.json()["capabilities"]["auth"] == "mtproto_session"

                manual_dm_ingest = await client.post(
                    f"/api/studio/channels/{channel.id}/sources/{dm_body['id']}/ingest",
                    headers=headers,
                )
                assert manual_dm_ingest.status_code == 409
                assert manual_dm_ingest.json()["detail"] == "Unsupported source adapter"

            async with Session() as session:
                connectors = (
                    await session.execute(
                        select(SourceConnector).where(SourceConnector.channel_id == channel.id)
                    )
                ).scalars().all()
                by_kind = {connector.kind: connector for connector in connectors}
                assert by_kind["telegram_channel_dms"].channel_id == channel.id
                assert by_kind["telegram_channel_dms"].legacy_ai_source_id is None
                assert by_kind["telegram"].legacy_ai_source_id is not None

                legacy_rows = (
                    await session.execute(
                        select(AISource).where(AISource.channel_id == channel.id)
                    )
                ).scalars().all()
                assert len(legacy_rows) == 1
                assert legacy_rows[0].source_type == "telegram"
                assert legacy_rows[0].source_value == "legacy-history-chat"
        finally:
            await engine.dispose()

    asyncio.run(run())
