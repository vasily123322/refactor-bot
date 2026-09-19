from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.admin_agent as admin_agent_api_module
import app.api.studio.app as studio_app_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.domain.content.models import ContentItem
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.ai_generation import AIGenerationService


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAGENT",
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


def test_studio_assistant_runs_are_owner_scoped_bounded_and_draft_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(admin_agent_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9701, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009701, "Owned")
                ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
                ai.enabled = False
                await session.commit()
                other = await ClientsRepo(session).create_or_get(9702, "other", "Other")
                foreign = await ChannelsRepo(session).create(other.id, -1009702, "Foreign")

            generation_calls = 0

            async def generated(self, **kwargs):
                nonlocal generation_calls
                generation_calls += 1
                assert kwargs["channel_id"] == channel.id
                return {
                    "success": True,
                    "text": json.dumps(
                        {
                            "drafts": [
                                {"title": "Первый черновик", "text": "Evergreen текст номер один."},
                                {"title": "Второй черновик", "text": "Evergreen текст номер два."},
                                {"title": "Третий черновик", "text": "Evergreen текст номер три."},
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    "tokens_used": 33,
                    "model": "provider/model",
                    "error": None,
                }

            monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)

            headers = {"X-Telegram-Init-Data": _init_data(9701)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                catalog = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/skills",
                    headers=headers,
                )
                assert catalog.status_code == 200
                skills = catalog.json()
                assert [(row["skill_id"], row["version"]) for row in skills] == [
                    ("attention_today", "1"),
                    ("drafts_tomorrow", "1"),
                ]
                attention_skill, draft_skill = skills
                assert attention_skill["capability_classes"] == ["read_only"]
                assert attention_skill["resumable"] is False
                assert attention_skill["approval_requirement"] == "none"
                assert attention_skill["operator_input_schema"]["additionalProperties"] is False
                assert draft_skill["capability_classes"] == ["draft_write"]
                assert draft_skill["resumable"] is True
                assert draft_skill["resume_policy"] == "explicit"
                assert draft_skill["execution_limits"]["max_llm_calls"] == 1
                assert draft_skill["execution_limits"]["max_seconds"] == 30.0
                assert "memory/profile" in draft_skill["context_requirements"]

                denied_catalog = await client.get(
                    f"/api/studio/channels/{foreign.id}/assistant/skills",
                    headers=headers,
                )
                assert denied_catalog.status_code == 404

                forged_skill = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "attention_today",
                        "skill_id": "unregistered",
                        "skill_version": "999",
                    },
                    headers=headers,
                )
                assert forged_skill.status_code == 422

                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={"scenario": "attention_today"},
                    headers=headers,
                )
                assert created.status_code == 201
                body = created.json()
                assert body["channel_id"] == channel.id
                assert body["scenario"] == "attention_today"
                assert body["request_id"] is None
                assert body["status"] == "completed"
                assert body["result"]["tool_names"] == [
                    "schedule_attention",
                    "publication_attention",
                    "source_health",
                    "ai_health",
                ]
                assert body["events"][0]["event_type"] == "run_started"

                listed = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/runs?limit=5",
                    headers=headers,
                )
                assert listed.status_code == 200
                assert listed.json()[0]["id"] == body["id"]
                assert listed.json()[0]["events"] == []

                fetched = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/runs/{body['id']}",
                    headers=headers,
                )
                assert fetched.status_code == 200
                assert fetched.json()["events"][-1]["event_type"] == "run_completed"

                denied = await client.post(
                    f"/api/studio/channels/{foreign.id}/assistant/runs",
                    json={"scenario": "attention_today"},
                    headers=headers,
                )
                assert denied.status_code == 404

                invalid = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={"scenario": "attention_today", "tool": "arbitrary_sql"},
                    headers=headers,
                )
                assert invalid.status_code == 422

                missing_key = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={"scenario": "drafts_tomorrow"},
                    headers=headers,
                )
                assert missing_key.status_code == 422

                forged_channel = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "drafts_tomorrow",
                        "request_id": "api-draft-0001",
                        "channel_id": foreign.id,
                    },
                    headers=headers,
                )
                assert forged_channel.status_code == 422

                drafts = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "drafts_tomorrow",
                        "request_id": "api-draft-0001",
                    },
                    headers=headers,
                )
                assert drafts.status_code == 201
                draft_body = drafts.json()
                assert draft_body["channel_id"] == channel.id
                assert draft_body["scenario"] == "drafts_tomorrow"
                assert draft_body["request_id"] == "api-draft-0001"
                assert draft_body["status"] == "completed"
                assert draft_body["result"]["draft_count"] == 3
                assert len(draft_body["result"]["drafts"]) == 3
                assert all(
                    item["status"] == "draft"
                    for item in draft_body["result"]["drafts"]
                )

                retry = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "drafts_tomorrow",
                        "request_id": "api-draft-0001",
                    },
                    headers=headers,
                )
                assert retry.status_code == 201
                assert retry.json()["id"] == draft_body["id"]
                assert retry.json()["result"] == draft_body["result"]
                assert generation_calls == 1

                foreign_reuse = await client.post(
                    f"/api/studio/channels/{foreign.id}/assistant/runs",
                    json={
                        "scenario": "drafts_tomorrow",
                        "request_id": "api-draft-0001",
                    },
                    headers=headers,
                )
                assert foreign_reuse.status_code == 404

            async with Session() as session:
                assert (
                    await session.execute(
                        select(func.count(ContentItem.id)).where(
                            ContentItem.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 3
        finally:
            await engine.dispose()

    asyncio.run(run())
