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
        "query_id": "AAESERIES",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Series",
                "username": f"series_{user_id}",
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


def _series_payload(count: int) -> str:
    return json.dumps(
        {
            "series": {
                "title": "Большая редакционная серия про понятные рабочие процессы",
                "summary": "Bounded evergreen series summary.",
            },
            "posts": [
                {
                    "title": f"Пост {index}: самостоятельная тема",
                    "angle": f"Отдельный угол {index}",
                    "objective": f"Практическая цель {index}",
                    "text": f"Evergreen content body {index} without current factual claims.",
                }
                for index in range(1, count + 1)
            ],
        },
        ensure_ascii=False,
    )


def test_studio_content_series_input_idempotency_and_owner_scope(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(admin_agent_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(12101, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -10012101, "Owned")
                ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
                ai.enabled = True
                ai.model = "provider/model"
                await session.commit()

                foreign_owner = await ClientsRepo(session).create_or_get(
                    12102,
                    "foreign",
                    "Foreign",
                )
                foreign = await ChannelsRepo(session).create(
                    foreign_owner.id,
                    -10012102,
                    "Foreign",
                )

            calls = 0

            async def generated(self, **kwargs):
                nonlocal calls
                calls += 1
                return {
                    "success": True,
                    "text": _series_payload(4),
                    "tokens_used": 21,
                    "model": "provider/model",
                    "error": None,
                }

            monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
            headers = {"X-Telegram-Init-Data": _init_data(12101)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)

            async with httpx.AsyncClient(transport=transport, base_url="http://studio") as client:
                forged = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-0001",
                        "skill_id": "prepare_content_series",
                        "skill_version": "1",
                        "operator_input": {
                            "brief": "Сделай полезную evergreen серию для выбранного канала.",
                            "post_count": 4,
                        },
                    },
                    headers=headers,
                )
                assert forged.status_code == 422

                denied = await client.post(
                    f"/api/studio/channels/{foreign.id}/assistant/runs",
                    json={
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-0002",
                        "operator_input": {
                            "brief": "Сделай полезную evergreen серию для выбранного канала.",
                            "post_count": 4,
                        },
                    },
                    headers=headers,
                )
                assert denied.status_code == 404

                invalid_payloads = [
                    {
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-1001",
                    },
                    {
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-1002",
                        "operator_input": {"post_count": 4},
                    },
                    {
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-1003",
                        "operator_input": {
                            "brief": "Слишком коротко",
                            "post_count": 4,
                        },
                    },
                    {
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-1004",
                        "operator_input": {
                            "brief": "А" * 2001,
                            "post_count": 4,
                        },
                    },
                    {
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-1005",
                        "operator_input": {
                            "brief": "Достаточно длинный editorial brief для bounded серии.",
                            "post_count": 1,
                        },
                    },
                    {
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-1006",
                        "operator_input": {
                            "brief": "Достаточно длинный editorial brief для bounded серии.",
                            "post_count": 9,
                        },
                    },
                    {
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-1007",
                        "operator_input": {
                            "brief": "Достаточно длинный editorial brief для bounded серии.",
                            "post_count": 4,
                            "publish_now": True,
                        },
                    },
                ]
                for payload in invalid_payloads:
                    response = await client.post(
                        f"/api/studio/channels/{channel.id}/assistant/runs",
                        json=payload,
                        headers=headers,
                    )
                    assert response.status_code == 422

                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-good-0001",
                        "operator_input": {
                            "brief": "   Сделай полезную evergreen серию для выбранного канала.   ",
                            "post_count": 4,
                        },
                    },
                    headers=headers,
                )
                assert created.status_code == 201
                body = created.json()
                assert body["scenario"] == "prepare_content_series"
                assert body["skill_id"] == "prepare_content_series"
                assert body["skill_version"] == "1"
                assert body["operator_input"] == {
                    "brief": "Сделай полезную evergreen серию для выбранного канала.",
                    "post_count": 4,
                }
                assert body["result"]["requested_post_count"] == 4
                assert len(body["result"]["posts"]) == 4
                assert all(row["status"] == "draft" for row in body["result"]["posts"])
                assert calls == 1

                retry = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-good-0001",
                        "operator_input": {
                            "brief": "Сделай полезную evergreen серию для выбранного канала.",
                            "post_count": 4,
                        },
                    },
                    headers=headers,
                )
                assert retry.status_code == 201
                assert retry.json()["id"] == body["id"]
                assert calls == 1

                conflict = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-good-0001",
                        "operator_input": {
                            "brief": "Сделай полезную evergreen серию для выбранного канала.",
                            "post_count": 3,
                        },
                    },
                    headers=headers,
                )
                assert conflict.status_code == 409
                assert calls == 1

            async with Session() as session:
                count = (
                    await session.execute(
                        select(func.count(ContentItem.id)).where(
                            ContentItem.channel_id == channel.id
                        )
                    )
                ).scalar_one()
                assert count == 4
        finally:
            await engine.dispose()

    asyncio.run(run())
