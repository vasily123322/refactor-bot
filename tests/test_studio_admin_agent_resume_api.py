from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.admin_agent as admin_agent_api_module
import app.api.studio.app as studio_app_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.admin_agent import AdminAgentRunner
from app.services.ai_generation import AIGenerationService


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAERESUME",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Assistant",
                "username": f"assistant_{user_id}",
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


def test_studio_resume_is_owner_scoped_server_directed_and_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(admin_agent_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9961, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009961, "Owned")
                ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
                ai.enabled = True
                ai.model = "provider/model"
                await session.commit()
                other = await ClientsRepo(session).create_or_get(9962, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009962, "Foreign")

            calls = 0

            async def generated(self, **kwargs):
                nonlocal calls
                calls += 1
                return {
                    "success": True,
                    "text": json.dumps(
                        {
                            "drafts": [
                                {"title": "A", "text": "Evergreen alpha"},
                                {"title": "B", "text": "Evergreen beta"},
                                {"title": "C", "text": "Evergreen gamma"},
                            ]
                        }
                    ),
                    "tokens_used": 10,
                    "model": "provider/model",
                    "error": None,
                }

            original_persist = AdminAgentRunner._persist_validated_drafts

            async def interrupted(self, run):
                raise RuntimeError("synthetic request interruption")

            monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
            monkeypatch.setattr(AdminAgentRunner, "_persist_validated_drafts", interrupted)

            headers = {"X-Telegram-Init-Data": _init_data(9961)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "drafts_tomorrow",
                        "request_id": "api-resume-0001",
                    },
                    headers=headers,
                )
                assert created.status_code == 201
                body = created.json()
                assert body["skill_id"] == "drafts_tomorrow"
                assert body["skill_version"] == "1"
                assert body["workflow_phase"] == "generation_validated"
                assert body["resumable"] is True
                assert body["resume_state"] == "available"
                assert calls == 1

                forged = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs/{body['id']}/resume",
                    json={"next_step": "publish_now"},
                    headers=headers,
                )
                assert forged.status_code == 422

                denied = await client.post(
                    f"/api/studio/channels/{foreign.id}/assistant/runs/{body['id']}/resume",
                    json={},
                    headers=headers,
                )
                assert denied.status_code == 404

                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_persist_validated_drafts",
                    original_persist,
                )
                resumed = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs/{body['id']}/resume",
                    json={},
                    headers=headers,
                )
                assert resumed.status_code == 200
                resumed_body = resumed.json()
                assert resumed_body["id"] == body["id"]
                assert resumed_body["status"] == "completed"
                assert resumed_body["workflow_phase"] == "completed"
                assert resumed_body["resumable"] is False
                assert resumed_body["resume_state"] == "completed"
                assert resumed_body["result"]["draft_count"] == 3
                assert calls == 1

                repeated = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs/{body['id']}/resume",
                    json={},
                    headers=headers,
                )
                assert repeated.status_code == 200
                assert repeated.json()["id"] == body["id"]
                assert repeated.json()["result"] == resumed_body["result"]
                assert calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
