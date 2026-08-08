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
from app.domain.sources.enrichment import CandidateEnrichmentRun
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.sources_v2 import SourcesRepo


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAACT",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Activity",
                "username": f"activity_{user_id}",
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


def test_studio_ai_activity_is_read_only_and_owner_scoped(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(candidate_actions_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(8401, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1008401, "AI Studio")
                other = await ClientsRepo(session).create_or_get(8402, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1008402, "Foreign")

                ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
                ai.enabled = True
                ai.model = "provider/model"
                ai.tokens_used_day = 321
                ai.tokens_used_month = 654
                await session.commit()

                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=channel.id,
                    kind="rss",
                    value="https://example.com/activity-api.xml",
                )
                document, _ = await repo.upsert_document(
                    connector=connector,
                    external_id="activity-api-entry",
                    content="SENSITIVE SOURCE BODY",
                )
                candidate = await repo.ensure_candidate(
                    source_document_id=document.id,
                    channel_id=channel.id,
                    suggested_action="research",
                )
                session.add(
                    CandidateEnrichmentRun(
                        candidate_id=candidate.id,
                        provider="channel_ai",
                        model="provider/model",
                        status="failed",
                        input_hash="d" * 64,
                        input_chars=21,
                        output={},
                        error="CandidateEnrichmentError",
                    )
                )
                await session.commit()

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(8401)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                response = await client.get(
                    f"/api/studio/channels/{channel.id}/ai/activity?limit=20",
                    headers=headers,
                )
                assert response.status_code == 200
                body = response.json()
                assert body["usage"]["enabled"] is True
                assert body["usage"]["model"] == "provider/model"
                assert body["usage"]["tokens_used_day"] == 321
                assert body["enrichment_counts"] == {"failed": 1}
                assert body["rewrite_counts"] == {}
                assert body["runs"][0]["kind"] == "enrichment"
                assert body["runs"][0]["error_type"] == "CandidateEnrichmentError"
                assert "SENSITIVE SOURCE BODY" not in json.dumps(body)

                foreign_response = await client.get(
                    f"/api/studio/channels/{foreign.id}/ai/activity",
                    headers=headers,
                )
                assert foreign_response.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(run())
