from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.candidate_structured_edit as candidate_structured_edit_module
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
        "query_id": "AAEAASE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Structured",
                "username": f"structured_{user_id}",
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


def test_candidate_structured_edit_route_is_mounted_and_owner_scoped(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(candidate_structured_edit_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9501, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009501, "Structured")
                other = await ClientsRepo(session).create_or_get(9502, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009502, "Foreign")
                await session.commit()
                channel_id = int(channel.id)
                foreign_id = int(foreign.id)

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            endpoint = (
                f"/api/studio/channels/{channel_id}/candidates/999999/"
                "rewrite/ai/structured/edit"
            )
            payload = {"run_id": 1, "operation": "shorten"}

            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                unauthenticated = await client.post(endpoint, json=payload)
                assert unauthenticated.status_code == 401
                assert unauthenticated.json()["detail"] == (
                    "Telegram Mini App init data is required"
                )

                headers = {"X-Telegram-Init-Data": _init_data(9501)}
                reachable = await client.post(endpoint, headers=headers, json=payload)
                assert reachable.status_code == 422
                assert reachable.json()["detail"] == (
                    "candidate structured rewrite is no longer current"
                )

                foreign_result = await client.post(
                    (
                        f"/api/studio/channels/{foreign_id}/candidates/999999/"
                        "rewrite/ai/structured/edit"
                    ),
                    headers=headers,
                    json=payload,
                )
                assert foreign_result.status_code == 404
                assert foreign_result.json()["detail"] == "Channel not found"
        finally:
            await engine.dispose()

    asyncio.run(run())
