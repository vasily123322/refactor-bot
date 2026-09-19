from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.admin_agent as admin_agent_api_module
import app.api.studio.app as studio_app_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAGENT",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Assistant",
                "username": f"assistant_{user_id}",
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


def test_studio_assistant_runs_are_owner_scoped_and_input_is_bounded(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(admin_agent_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9701, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009701, "Owned")
                other = await ClientsRepo(session).create_or_get(9702, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009702, "Foreign")

            headers = {"X-Telegram-Init-Data": _init_data(9701)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={"scenario": "attention_today"},
                    headers=headers,
                )
                assert created.status_code == 201
                body = created.json()
                assert body["channel_id"] == channel.id
                assert body["scenario"] == "attention_today"
                assert body["status"] == "completed"
                assert body["result"]["tool_names"] == [
                    "schedule_attention",
                    "publication_attention",
                    "source_health",
                    "ai_health",
                ]
                assert body["events"][0]["event_type"] == "run_started"

                listed = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/runs?limit=5",
                    headers=headers,
                )
                assert listed.status_code == 200
                assert listed.json()[0]["id"] == body["id"]
                assert listed.json()[0]["events"] == []

                fetched = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/runs/{body['id']}",
                    headers=headers,
                )
                assert fetched.status_code == 200
                assert fetched.json()["events"][-1]["event_type"] == "run_completed"

                denied = await client.post(
                    f"/api/studio/channels/{foreign.id}/assistant/runs",
                    json={"scenario": "attention_today"},
                    headers=headers,
                )
                assert denied.status_code == 404

                invalid = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={"scenario": "attention_today", "tool": "arbitrary_sql"},
                    headers=headers,
                )
                assert invalid.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(run())
