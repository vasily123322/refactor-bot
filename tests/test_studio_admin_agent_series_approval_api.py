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
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.ai_generation import AIGenerationService


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AASERIESAPPROVAL",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "SeriesApproval",
                "username": f"series_approval_{user_id}",
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


def _series_payload(count: int = 3) -> str:
    return json.dumps(
        {
            "series": {
                "title": "API scheduling series",
                "summary": "Evergreen API series.",
            },
            "posts": [
                {
                    "title": f"API post {ordinal}",
                    "angle": f"Angle {ordinal}",
                    "objective": f"Objective {ordinal}",
                    "text": f"Evergreen API body {ordinal}.",
                }
                for ordinal in range(1, count + 1)
            ],
        },
        ensure_ascii=False,
    )


def _slots(*, minute: int = 0) -> list[dict[str, object]]:
    return [
        {
            "ordinal": ordinal,
            "local_date": "2099-09-20",
            "local_time": f"{12 + ordinal:02d}:{minute:02d}",
        }
        for ordinal in range(1, 4)
    ]


def test_studio_series_approval_api_is_owner_scoped_and_bounded(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(admin_agent_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(13101, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -10013101, "Owned")
                ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
                ai.enabled = True
                ai.model = "provider/model"
                foreign_owner = await ClientsRepo(session).create_or_get(
                    13102,
                    "foreign",
                    "Foreign",
                )
                foreign_channel = await ChannelsRepo(session).create(
                    foreign_owner.id,
                    -10013102,
                    "Foreign",
                )
                await session.commit()

            async def generated(self, **kwargs):
                return {
                    "success": True,
                    "text": _series_payload(3),
                    "tokens_used": 15,
                    "model": "provider/model",
                    "error": None,
                }

            monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
            headers = {"X-Telegram-Init-Data": _init_data(13101)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://studio",
            ) as client:
                run_response = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    json={
                        "scenario": "prepare_content_series",
                        "request_id": "series-api-source-0001",
                        "operator_input": {
                            "brief": "Подготовь три evergreen поста для API approval теста.",
                            "post_count": 3,
                        },
                    },
                    headers=headers,
                )
                assert run_response.status_code == 201
                source = run_response.json()
                assert source["status"] == "completed"

                denied = await client.post(
                    f"/api/studio/channels/{foreign_channel.id}/assistant/series-approvals",
                    json={
                        "source_run_id": source["id"],
                        "request_id": "series-api-denied-0001",
                        "slots": _slots(),
                    },
                    headers=headers,
                )
                assert denied.status_code == 404

                injected = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/series-approvals",
                    json={
                        "source_run_id": source["id"],
                        "request_id": "series-api-injected-0001",
                        "slots": _slots(),
                        "timezone": "Pacific/Auckland",
                        "content_item_ids": [1, 2, 3],
                        "content_revisions": [99, 99, 99],
                        "state": "executed",
                    },
                    headers=headers,
                )
                assert injected.status_code == 422

                injected_slot = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/series-approvals",
                    json={
                        "source_run_id": source["id"],
                        "request_id": "series-api-injected-0002",
                        "slots": [
                            {**slot, "content_item_id": 999}
                            for slot in _slots()
                        ],
                    },
                    headers=headers,
                )
                assert injected_slot.status_code == 422

                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/series-approvals",
                    json={
                        "source_run_id": source["id"],
                        "request_id": "series-api-proposal-0001",
                        "slots": _slots(),
                    },
                    headers=headers,
                )
                assert created.status_code == 201
                body = created.json()
                assert body["state"] == "pending_review"
                assert body["action_type"] == "schedule_content_series"
                assert body["source_run_id"] == source["id"]
                assert body["item_count"] == 3
                assert body["timezone"] == "UTC+3"
                assert [item["ordinal"] for item in body["items"]] == [1, 2, 3]
                assert all(item["captured_content_revision"] == 1 for item in body["items"])
                assert all(item["schedule_entry_id"] is None for item in body["items"])
                assert all(item["publication_id"] is None for item in body["items"])

                same = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/series-approvals",
                    json={
                        "source_run_id": source["id"],
                        "request_id": "series-api-proposal-0001",
                        "slots": _slots(),
                    },
                    headers=headers,
                )
                assert same.status_code == 201
                assert same.json()["id"] == body["id"]

                changed = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/series-approvals",
                    json={
                        "source_run_id": source["id"],
                        "request_id": "series-api-proposal-0001",
                        "slots": _slots(minute=15),
                    },
                    headers=headers,
                )
                assert changed.status_code == 409

                listing = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/series-approvals",
                    headers=headers,
                )
                assert listing.status_code == 200
                assert [row["id"] for row in listing.json()] == [body["id"]]

                fetched = await client.get(
                    (
                        f"/api/studio/channels/{channel.id}/assistant/"
                        f"series-approvals/{body['id']}"
                    ),
                    headers=headers,
                )
                assert fetched.status_code == 200
                assert fetched.json()["items"] == body["items"]

                rejected = await client.post(
                    (
                        f"/api/studio/channels/{channel.id}/assistant/"
                        f"series-approvals/{body['id']}/reject"
                    ),
                    json={},
                    headers=headers,
                )
                assert rejected.status_code == 200
                assert rejected.json()["state"] == "rejected"

                executable = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/series-approvals",
                    json={
                        "source_run_id": source["id"],
                        "request_id": "series-api-proposal-0002",
                        "slots": _slots(minute=30),
                    },
                    headers=headers,
                )
                assert executable.status_code == 201
                executable_id = executable.json()["id"]

                invalid_decision = await client.post(
                    (
                        f"/api/studio/channels/{channel.id}/assistant/"
                        f"series-approvals/{executable_id}/approve"
                    ),
                    json={"execution_plan": []},
                    headers=headers,
                )
                assert invalid_decision.status_code == 422

                executed = await client.post(
                    (
                        f"/api/studio/channels/{channel.id}/assistant/"
                        f"series-approvals/{executable_id}/approve"
                    ),
                    json={},
                    headers=headers,
                )
                assert executed.status_code == 200
                executed_body = executed.json()
                assert executed_body["state"] == "executed"
                assert all(
                    item["schedule_entry_id"] is not None
                    and item["publication_id"] is not None
                    and item["state"] == "executed"
                    for item in executed_body["items"]
                )

                second_approve = await client.post(
                    (
                        f"/api/studio/channels/{channel.id}/assistant/"
                        f"series-approvals/{executable_id}/approve"
                    ),
                    json={},
                    headers=headers,
                )
                assert second_approve.status_code == 200
                assert second_approve.json()["items"] == executed_body["items"]

                reject_after_execution = await client.post(
                    (
                        f"/api/studio/channels/{channel.id}/assistant/"
                        f"series-approvals/{executable_id}/reject"
                    ),
                    json={},
                    headers=headers,
                )
                assert reject_after_execution.status_code == 409

            async with Session() as session:
                assert (
                    await session.execute(
                        select(func.count(ScheduleEntry.id)).where(
                            ScheduleEntry.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 3
                assert (
                    await session.execute(
                        select(func.count(Publication.id)).where(
                            Publication.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 3
                assert (
                    await session.execute(
                        select(func.count(PublicationAttempt.id))
                    )
                ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
