from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.candidate_media as candidate_media_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import MediaAssetsRepo
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


def test_candidate_media_api_is_owner_scoped_and_sanitized(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(candidate_media_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9401, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009401, "Media")
                other = await ClientsRepo(session).create_or_get(9402, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009402, "Foreign")
                connector = await SourcesRepo(session).create_connector(
                    channel_id=channel.id,
                    kind="telegram",
                    value="@source",
                )
                document, _ = await SourcesRepo(session).upsert_document(
                    connector=connector,
                    external_id="telegram:-1001:55",
                    content="[Telegram video]",
                    metadata={
                        "telegram_media": {
                            "kind": "video",
                            "mime_type": "video/mp4\n",
                            "size_bytes": 2_621_440,
                            "width": 1920,
                            "height": 1080,
                            "duration_seconds": 18,
                            "file_reference": "must-not-leak",
                            "access_hash": 123,
                        }
                    },
                )
                candidate = await SourcesRepo(session).ensure_candidate(
                    source_document_id=document.id,
                    channel_id=channel.id,
                )

                dismissed_document, _ = await SourcesRepo(session).upsert_document(
                    connector=connector,
                    external_id="telegram:-1001:56",
                    content="[Telegram photo]",
                    metadata={
                        "telegram_media": {
                            "kind": "photo",
                            "mime_type": "image/jpeg",
                            "size_bytes": 123_456,
                        }
                    },
                )
                dismissed_candidate = await SourcesRepo(session).ensure_candidate(
                    source_document_id=dismissed_document.id,
                    channel_id=channel.id,
                )
                dismissed_candidate = await SourcesRepo(session).set_candidate_status(
                    dismissed_candidate,
                    "dismissed",
                )

                foreign_connector = await SourcesRepo(session).create_connector(
                    channel_id=foreign.id,
                    kind="telegram",
                    value="@foreign-source",
                )
                foreign_document, _ = await SourcesRepo(session).upsert_document(
                    connector=foreign_connector,
                    external_id="telegram:-2001:77",
                    content="[Telegram photo]",
                    metadata={
                        "telegram_media": {
                            "kind": "photo",
                            "mime_type": "image/jpeg",
                            "size_bytes": 222_222,
                        }
                    },
                )
                foreign_candidate = await SourcesRepo(session).ensure_candidate(
                    source_document_id=foreign_document.id,
                    channel_id=foreign.id,
                )

                asset = await MediaAssetsRepo(session).create(
                    channel_id=channel.id,
                    kind="video",
                    source="telegram_source",
                    telegram_file_id="opaque-bot-file-id",
                    mime_type="video/mp4",
                )
                document.meta = {
                    **dict(document.meta or {}),
                    "media_asset_id": int(asset.id),
                }
                await session.commit()
                channel_id = int(channel.id)
                foreign_id = int(foreign.id)
                candidate_id = int(candidate.id)
                dismissed_candidate_id = int(dismissed_candidate.id)
                foreign_candidate_id = int(foreign_candidate.id)
                document_id = int(document.id)
                asset_id = int(asset.id)

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(9401)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                response = await client.get(
                    f"/api/studio/channels/{channel_id}/candidate-media",
                    headers=headers,
                )
                assert response.status_code == 200
                assert response.json() == [
                    {
                        "candidate_id": candidate_id,
                        "source_document_id": document_id,
                        "kind": "video",
                        "mime_type": "video/mp4",
                        "size_bytes": 2_621_440,
                        "width": 1920,
                        "height": 1080,
                        "duration_seconds": 18,
                        "promotable": True,
                        "media_asset_id": asset_id,
                    }
                ]
                batch = await client.get(
                    f"/api/studio/channels/{channel_id}/candidate-media",
                    headers=headers,
                    params={
                        "candidate_ids": (
                            f"{candidate_id},{dismissed_candidate_id},{foreign_candidate_id}"
                        )
                    },
                )
                assert batch.status_code == 200
                assert [row["candidate_id"] for row in batch.json()] == [candidate_id]

                invalid_batch = await client.get(
                    f"/api/studio/channels/{channel_id}/candidate-media",
                    headers=headers,
                    params={"candidate_ids": "not-an-id"},
                )
                assert invalid_batch.status_code == 422

                serialized = json.dumps(response.json())
                assert "file_reference" not in serialized
                assert "access_hash" not in serialized
                assert "must-not-leak" not in serialized
                assert "opaque-bot-file-id" not in serialized

                forbidden = await client.get(
                    f"/api/studio/channels/{foreign_id}/candidate-media",
                    headers=headers,
                )
                assert forbidden.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(run())
