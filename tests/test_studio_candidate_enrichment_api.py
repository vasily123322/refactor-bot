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
from app.services.candidate_enrichment import EnrichmentInput, EnrichmentOutput


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAI",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Enrichment",
                "username": f"enrichment_{user_id}",
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


class _AIProvider:
    name = "test_ai"
    model = "fixture-model"

    async def enrich(self, payload: EnrichmentInput) -> EnrichmentOutput:
        assert "source body" in payload.text
        return EnrichmentOutput(
            summary="Independent AI summary",
            topic="AI topic",
            score=0.91,
            metadata={"score_kind": "fixture"},
        )


class _AIFactory:
    def __init__(self, session) -> None:
        self.session = session

    async def build(self, channel_id: int):
        assert channel_id > 0
        return _AIProvider()


def test_studio_candidate_enrichment_is_owner_scoped_and_reuses_runs(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(candidate_actions_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(
                candidate_actions_module,
                "ChannelAIEnrichmentProviderFactory",
                _AIFactory,
            )

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(8201, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1008201, "Inbox")
                other = await ClientsRepo(session).create_or_get(8202, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1008202, "Foreign")
                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=channel.id,
                    kind="rss",
                    value="https://example.com/feed.xml",
                    reuse_policy="summarize",
                )
                document, _ = await repo.upsert_document(
                    connector=connector,
                    external_id="enrichment-entry",
                    title="Interesting source",
                    content="First source body sentence. Second source body sentence.",
                    source_url="https://example.com/article",
                    metadata={"reuse_policy": "summarize"},
                )
                candidate = await repo.ensure_candidate(
                    source_document_id=document.id,
                    channel_id=channel.id,
                    suggested_action="summarize",
                    metadata={"reuse_policy": "summarize"},
                )

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(8201)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                local = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/enrich/local",
                    headers=headers,
                    json={},
                )
                assert local.status_code == 200
                local_body = local.json()
                assert local_body["provider"] == "local"
                assert local_body["run_status"] == "completed"
                assert local_body["summary"].startswith("First source body sentence")
                assert local_body["reused_existing"] is False

                local_again = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/enrich/local",
                    headers=headers,
                    json={},
                )
                assert local_again.status_code == 200
                assert local_again.json()["run_id"] == local_body["run_id"]
                assert local_again.json()["reused_existing"] is True

                ai = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/enrich/ai",
                    headers=headers,
                    json={},
                )
                assert ai.status_code == 200
                ai_body = ai.json()
                assert ai_body["provider"] == "test_ai"
                assert ai_body["model"] == "fixture-model"
                assert ai_body["summary"] == "Independent AI summary"
                assert ai_body["topic"] == "AI topic"
                assert ai_body["score"] == 0.91

                foreign_result = await client.post(
                    f"/api/studio/channels/{foreign.id}/candidates/{candidate.id}/enrich/local",
                    headers=headers,
                    json={},
                )
                assert foreign_result.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(run())
