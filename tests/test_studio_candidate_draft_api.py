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
import app.api.studio.candidate_actions as candidate_actions_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
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
                "first_name": "Draft",
                "username": f"draft_{user_id}",
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


def test_candidate_to_draft_api_is_owner_scoped_and_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(candidate_actions_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(8101, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1008101, "Draft channel")
                other = await ClientsRepo(session).create_or_get(8102, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1008102, "Foreign")
                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=channel.id,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    reuse_policy="reference_only",
                )
                document, _ = await repo.upsert_document(
                    connector=connector,
                    external_id="draft-entry",
                    title="Source draft",
                    content="SOURCE BODY MUST NOT BE COPIED",
                    source_url="https://example.com/article",
                )
                candidate = await repo.ensure_candidate(
                    source_document_id=document.id,
                    channel_id=channel.id,
                    suggested_action="research",
                    metadata={"reuse_policy": "reference_only"},
                )

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(8101)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                first = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/draft",
                    headers=headers,
                    json={},
                )
                assert first.status_code == 200
                first_body = first.json()
                text = first_body["document"]["blocks"][0]["text"]
                assert "SOURCE BODY MUST NOT BE COPIED" not in text
                assert "https://example.com/article" in text

                second = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/draft",
                    headers=headers,
                    json={},
                )
                assert second.status_code == 200
                assert second.json()["id"] == first_body["id"]

                foreign_result = await client.post(
                    f"/api/studio/channels/{foreign.id}/candidates/{candidate.id}/draft",
                    headers=headers,
                    json={},
                )
                assert foreign_result.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(run())
