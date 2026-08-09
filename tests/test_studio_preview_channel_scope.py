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
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Preview",
                "username": f"preview_{user_id}",
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


def test_exact_preview_channel_context_is_owner_scoped_before_delivery(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(8101, "owner", "Owner")
                await ChannelsRepo(session).create(owner.id, -1008101, "Owned")
                other = await ClientsRepo(session).create_or_get(8102, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1008102, "Foreign")

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                response = await client.post(
                    "/api/studio/preview/telegram",
                    headers={"X-Telegram-Init-Data": _init_data(8101)},
                    json={
                        "channel_id": int(foreign.id),
                        "replace_message_ids": [],
                        "document": {
                            "schema_version": 1,
                            "mode": "rich",
                            "blocks": [
                                {"id": "p", "type": "paragraph", "content": "No delivery"}
                            ],
                            "telegram": {},
                            "metadata": {},
                        },
                    },
                )
                assert response.status_code == 404
                assert response.json()["detail"] == "Channel not found"
        finally:
            await engine.dispose()

    asyncio.run(run())
