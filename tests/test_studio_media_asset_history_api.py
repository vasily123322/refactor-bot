from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.media_assets as media_assets_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import MediaAssetsRepo


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Media History",
                "username": f"media_history_{user_id}",
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


def test_media_asset_history_cursor_is_deterministic(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(media_assets_module, "AsyncSessionLocal", Session)

            same_created_at = datetime(2026, 9, 19, 13, 0, 0)
            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(
                    9401, "media_history", "Media History"
                )
                channel = await ChannelsRepo(session).create(
                    owner.id, -1009401, "Media History"
                )
                repo = MediaAssetsRepo(session)
                rows = []
                for index in range(4):
                    row = await repo.create(
                        channel_id=channel.id,
                        kind="photo",
                        source="test",
                        telegram_file_id=f"asset-{index}",
                    )
                    row.created_at = same_created_at
                    rows.append(row)
                await session.commit()
                expected_ids = sorted((int(row.id) for row in rows), reverse=True)

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9401)}
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://studio",
            ) as client:
                first = await client.get(
                    f"/api/studio/channels/{channel.id}/media-assets",
                    headers=headers,
                    params={"limit": 2},
                )
                assert first.status_code == 200
                first_rows = first.json()
                assert [row["id"] for row in first_rows] == expected_ids[:2]

                cursor = first_rows[-1]
                second = await client.get(
                    f"/api/studio/channels/{channel.id}/media-assets",
                    headers=headers,
                    params={
                        "limit": 2,
                        "before_created_at": cursor["created_at"],
                        "before_id": cursor["id"],
                    },
                )
                assert second.status_code == 200
                assert [row["id"] for row in second.json()] == expected_ids[2:]

                incomplete = await client.get(
                    f"/api/studio/channels/{channel.id}/media-assets",
                    headers=headers,
                    params={"before_id": expected_ids[0]},
                )
                assert incomplete.status_code == 422
                assert "provided together" in incomplete.json()["detail"]
        finally:
            await engine.dispose()

    asyncio.run(run())
