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
from app.services.candidate_rewrite import RewriteInput, RewriteOutput


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAAAR",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Rewrite",
                "username": f"rewrite_{user_id}",
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


class _RewriteProvider:
    name = "test_ai"
    model = "rewrite-fixture"

    def __init__(self) -> None:
        self.calls = 0

    async def rewrite(self, payload: RewriteInput) -> RewriteOutput:
        self.calls += 1
        assert payload.reuse_policy == "rewrite_with_attribution"
        assert "SOURCE BODY" in payload.text
        return RewriteOutput(
            text="Independent rewritten post for Telegram.",
            metadata={"generation_kind": "fixture"},
        )


class _RewriteFactory:
    provider = _RewriteProvider()

    def __init__(self, session) -> None:
        self.session = session

    async def build(self, channel_id: int):
        assert channel_id > 0
        return self.provider


def test_studio_ai_rewrite_is_owner_scoped_reused_and_consumed_by_draft(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(candidate_actions_module, "AsyncSessionLocal", Session)
            _RewriteFactory.provider = _RewriteProvider()
            monkeypatch.setattr(
                candidate_actions_module,
                "ChannelAIRewriteProviderFactory",
                _RewriteFactory,
            )

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(8301, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1008301, "Rewrite")
                other = await ClientsRepo(session).create_or_get(8302, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1008302, "Foreign")
                repo = SourcesRepo(session)
                connector = await repo.create_connector(
                    channel_id=channel.id,
                    kind="rss",
                    value="https://example.com/rewrite.xml",
                    reuse_policy="rewrite_with_attribution",
                )
                document, _ = await repo.upsert_document(
                    connector=connector,
                    external_id="rewrite-entry",
                    title="Source title",
                    content="SOURCE BODY MUST NOT APPEAR IN THE DRAFT.",
                    source_url="https://example.com/article",
                    metadata={"reuse_policy": "rewrite_with_attribution"},
                )
                candidate = await repo.ensure_candidate(
                    source_document_id=document.id,
                    channel_id=channel.id,
                    suggested_action="rewrite",
                    metadata={"reuse_policy": "rewrite_with_attribution"},
                )

            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Telegram-Init-Data": _init_data(8301)}
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                first = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/rewrite/ai",
                    headers=headers,
                    json={},
                )
                assert first.status_code == 200
                first_body = first.json()
                assert first_body["provider"] == "test_ai"
                assert first_body["model"] == "rewrite-fixture"
                assert first_body["run_status"] == "completed"
                assert first_body["text"] == "Independent rewritten post for Telegram."
                assert first_body["reused_existing"] is False

                second = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/rewrite/ai",
                    headers=headers,
                    json={},
                )
                assert second.status_code == 200
                assert second.json()["run_id"] == first_body["run_id"]
                assert second.json()["reused_existing"] is True
                assert _RewriteFactory.provider.calls == 1

                foreign_result = await client.post(
                    f"/api/studio/channels/{foreign.id}/candidates/{candidate.id}/rewrite/ai",
                    headers=headers,
                    json={},
                )
                assert foreign_result.status_code == 404

                draft = await client.post(
                    f"/api/studio/channels/{channel.id}/candidates/{candidate.id}/draft",
                    headers=headers,
                    json={},
                )
                assert draft.status_code == 200
                draft_body = draft.json()
                block_text = draft_body["document"]["blocks"][0]["text"]
                assert "Independent rewritten post for Telegram." in block_text
                assert "https://example.com/article" in block_text
                assert "SOURCE BODY MUST NOT APPEAR" not in block_text
                assert draft_body["document"]["metadata"]["rewrite_run_id"] == first_body["run_id"]
        finally:
            await engine.dispose()

    asyncio.run(run())
