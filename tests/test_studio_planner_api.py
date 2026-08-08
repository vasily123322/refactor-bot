from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.app as studio_app_module
import app.api.studio.planner as planner_api_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.domain.content import PostDocument
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Planner",
                "username": f"planner_{user_id}",
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


def test_studio_planner_is_owner_scoped_and_mutates_pending_schedule(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(planner_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(3001, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1003001, "Owner channel")
                other = await ClientsRepo(session).create_or_get(3002, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1003002, "Foreign")
                item = await ContentRepo(session).create(
                    channel_id=channel.id,
                    title="API Planner",
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Scheduled"}]
                    ),
                )
                when = datetime.now(timezone.utc) + timedelta(days=2)
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=item.id,
                    scheduled_at=when,
                )
                schedule_id = int(publication.schedule_entry_id)

            headers = {"X-Telegram-Init-Data": _init_data(3001)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                end = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
                listed = await client.get(
                    f"/api/studio/channels/{channel.id}/planner",
                    params={"start": start, "end": end},
                    headers=headers,
                )
                assert listed.status_code == 200
                assert listed.json()[0]["schedule_id"] == schedule_id
                assert listed.json()[0]["content_title"] == "API Planner"

                foreign_result = await client.get(
                    f"/api/studio/channels/{foreign.id}/planner",
                    params={"start": start, "end": end},
                    headers=headers,
                )
                assert foreign_result.status_code == 404

                moved = datetime.now(timezone.utc) + timedelta(days=3)
                rescheduled = await client.post(
                    f"/api/studio/channels/{channel.id}/planner/{schedule_id}/reschedule",
                    json={"scheduled_at": moved.isoformat(), "timezone": "UTC"},
                    headers=headers,
                )
                assert rescheduled.status_code == 200
                assert rescheduled.json()["timezone"] == "UTC"

                cancelled = await client.post(
                    f"/api/studio/channels/{channel.id}/planner/{schedule_id}/cancel",
                    json={},
                    headers=headers,
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["schedule_status"] == "cancelled"
                assert cancelled.json()["publication_status"] == "cancelled"
        finally:
            await engine.dispose()

    asyncio.run(run())
