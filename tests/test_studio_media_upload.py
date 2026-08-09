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
from app.services.telegram_media_upload import TelegramMediaUploadResult


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Upload",
                "username": f"upload_{user_id}",
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


class _UploadService:
    calls: list[dict] = []

    def __init__(self, bot) -> None:
        self.bot = bot

    async def upload(self, **kwargs):
        type(self).calls.append(kwargs)
        return TelegramMediaUploadResult(
            telegram_file_id="captured-file-id",
            width=1280,
            height=720,
            duration_seconds=15,
        )


def test_browser_upload_is_owner_scoped_and_persists_captured_file_id(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(media_assets_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(media_assets_module, "TelegramMediaUploadService", _UploadService)
            _UploadService.calls = []

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9401, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009401, "Upload")
                other = await ClientsRepo(session).create_or_get(9402, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009402, "Foreign")

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9401)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                response = await client.post(
                    f"/api/studio/channels/{channel.id}/media-assets/upload",
                    headers=headers,
                    data={"kind": "video", "label": "Browser clip"},
                    files={"file": ("../clip.mp4", b"browser-video", "video/mp4")},
                )
                assert response.status_code == 201
                payload = response.json()
                assert payload["kind"] == "video"
                assert payload["source"] == "studio_upload"
                assert payload["transport"] == "telegram"
                assert payload["label"] == "Browser clip"
                assert payload["width"] == 1280
                assert payload["height"] == 720
                assert payload["duration_seconds"] == 15
                assert "captured-file-id" not in json.dumps(payload)

                assert len(_UploadService.calls) == 1
                call = _UploadService.calls[0]
                assert call["tg_user_id"] == 9401
                assert call["kind"] == "video"
                assert call["data"] == b"browser-video"
                assert call["filename"] == "clip.mp4"

                blocked = await client.post(
                    f"/api/studio/channels/{foreign.id}/media-assets/upload",
                    headers=headers,
                    data={"kind": "photo"},
                    files={"file": ("cover.jpg", b"photo", "image/jpeg")},
                )
                assert blocked.status_code == 404
                assert len(_UploadService.calls) == 1

            async with Session() as session:
                asset = (await session.execute(select(MediaAsset))).scalar_one()
                assert asset.telegram_file_id == "captured-file-id"
                assert asset.channel_id == channel.id
                assert asset.size_bytes == len(b"browser-video")
                assert asset.mime_type == "video/mp4"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_browser_upload_rejects_empty_and_oversized_body_before_telegram(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(media_assets_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(media_assets_module, "TelegramMediaUploadService", _UploadService)
            monkeypatch.setattr(media_assets_module, "MAX_STUDIO_MEDIA_UPLOAD_BYTES", 4)
            _UploadService.calls = []

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9501, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009501, "Upload")

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9501)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                empty = await client.post(
                    f"/api/studio/channels/{channel.id}/media-assets/upload",
                    headers=headers,
                    data={"kind": "photo"},
                    files={"file": ("empty.jpg", b"", "image/jpeg")},
                )
                assert empty.status_code == 422

                large = await client.post(
                    f"/api/studio/channels/{channel.id}/media-assets/upload",
                    headers=headers,
                    data={"kind": "photo"},
                    files={"file": ("large.jpg", b"12345", "image/jpeg")},
                )
                assert large.status_code == 413
                assert _UploadService.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
