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

import app.api.studio.media_assets as media_assets_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.domain.content.models import MediaAsset
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
                "first_name": "Media",
                "username": f"media_{user_id}",
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


def test_media_asset_api_is_owner_scoped_and_never_exposes_transport_reference(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(media_assets_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9201, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009201, "Media")
                other = await ClientsRepo(session).create_or_get(9202, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009202, "Foreign")

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9201)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                telegram_asset = await client.post(
                    f"/api/studio/channels/{channel.id}/media-assets",
                    headers=headers,
                    json={
                        "kind": "photo",
                        "telegram_file_id": "opaque-telegram-file-id",
                        "label": "Cover",
                        "width": 1200,
                        "height": 630,
                    },
                )
                assert telegram_asset.status_code == 201
                payload = telegram_asset.json()
                assert payload["transport"] == "telegram"
                assert payload["label"] == "Cover"
                assert payload["kind"] == "photo"
                assert "telegram_file_id" not in payload
                assert "storage_url" not in payload
                assert "opaque-telegram-file-id" not in json.dumps(payload)

                https_asset = await client.post(
                    f"/api/studio/channels/{channel.id}/media-assets",
                    headers=headers,
                    json={
                        "kind": "video",
                        "storage_url": "https://cdn.example.test/video.mp4",
                        "duration_seconds": 12,
                    },
                )
                assert https_asset.status_code == 201
                assert https_asset.json()["transport"] == "https"
                assert "cdn.example.test" not in json.dumps(https_asset.json())

                listed = await client.get(
                    f"/api/studio/channels/{channel.id}/media-assets",
                    headers=headers,
                )
                assert listed.status_code == 200
                assert [row["id"] for row in listed.json()] == [
                    https_asset.json()["id"],
                    telegram_asset.json()["id"],
                ]

                forbidden = await client.get(
                    f"/api/studio/channels/{foreign.id}/media-assets",
                    headers=headers,
                )
                assert forbidden.status_code == 404

            async with Session() as session:
                rows = list((await session.execute(select(MediaAsset))).scalars().all())
                assert len(rows) == 2
                by_source = {str(row.source): row for row in rows}
                assert by_source["studio_telegram"].telegram_file_id == "opaque-telegram-file-id"
                assert by_source["studio_https"].storage_url == "https://cdn.example.test/video.mp4"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_media_asset_api_rejects_ambiguous_or_unsafe_transport(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(media_assets_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9301, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009301, "Media")

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9301)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                cases = [
                    {
                        "kind": "photo",
                        "telegram_file_id": "file-id",
                        "storage_url": "https://cdn.example.test/image.jpg",
                    },
                    {"kind": "photo"},
                    {"kind": "photo", "storage_url": "http://example.test/image.jpg"},
                    {
                        "kind": "photo",
                        "storage_url": "https://user:password@example.test/image.jpg",
                    },
                    {"kind": "photo", "telegram_file_id": "bad file id"},
                ]
                for body in cases:
                    response = await client.post(
                        f"/api/studio/channels/{channel.id}/media-assets",
                        headers=headers,
                        json=body,
                    )
                    assert response.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(run())
