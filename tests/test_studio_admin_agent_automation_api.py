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
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo


def _init_data(user_id: int) -> str:
    auth_date = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAEAUTOMATION",
        "user": json.dumps(
            {
                "id": user_id,
                "is_bot": False,
                "first_name": "Automation",
                "username": f"automation_{user_id}",
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


def test_studio_automation_api_is_owner_scoped_closed_and_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(studio_app_module, "AsyncSessionLocal", Session)
            monkeypatch.setattr(admin_agent_api_module, "AsyncSessionLocal", Session)

            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(
                    17001, "owner", "Owner"
                )
                channel = await ChannelsRepo(session).create(
                    owner.id, -10017001, "Owned"
                )
                other = await ClientsRepo(session).create_or_get(
                    17002, "other", "Other"
                )
                foreign = await ChannelsRepo(session).create(
                    other.id, -10017002, "Foreign"
                )

            headers = {"X-Telegram-Init-Data": _init_data(17001)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://studio",
            ) as client:
                catalog = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/skills",
                    headers=headers,
                )
                assert catalog.status_code == 200
                skills = catalog.json()
                assert {
                    (row["skill_id"], row["version"])
                    for row in skills
                    if row["automation_allowed"]
                } == {
                    ("attention_today", "1"),
                    ("drafts_tomorrow", "1"),
                    ("prepare_content_series", "1"),
                }
                assert all(
                    row["automation_policy"] == "bounded"
                    for row in skills
                    if row["automation_allowed"]
                )

                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-automation-0001",
                        "skill_id": "attention_today",
                        "skill_version": "1",
                        "operator_input": {},
                        "cadence": {
                            "kind": "daily",
                            "local_time": "09:30",
                        },
                    },
                )
                assert created.status_code == 201
                body = created.json()
                assert body["skill_id"] == "attention_today"
                assert body["skill_version"] == "1"
                assert body["operator_input"] == {}
                assert body["cadence"] == {
                    "kind": "daily",
                    "local_time": "09:30",
                    "weekday": None,
                }
                assert body["timezone"] == "UTC+3"
                assert body["enabled"] is True
                assert body["next_run_at"]

                retry = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-automation-0001",
                        "skill_id": "attention_today",
                        "skill_version": "1",
                        "operator_input": {},
                        "cadence": {
                            "kind": "daily",
                            "local_time": "09:30",
                        },
                    },
                )
                assert retry.status_code == 201
                assert retry.json()["id"] == body["id"]

                conflict = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-automation-0001",
                        "skill_id": "attention_today",
                        "skill_version": "1",
                        "operator_input": {},
                        "cadence": {
                            "kind": "daily",
                            "local_time": "10:30",
                        },
                    },
                )
                assert conflict.status_code == 409

                series = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-automation-series-0001",
                        "skill_id": "prepare_content_series",
                        "skill_version": "1",
                        "operator_input": {
                            "brief": "  Сделай bounded evergreen серию о рабочих процессах.  ",
                            "post_count": 3,
                        },
                        "cadence": {
                            "kind": "weekly",
                            "local_time": "11:15",
                            "weekday": 2,
                        },
                    },
                )
                assert series.status_code == 201
                assert series.json()["operator_input"]["brief"] == (
                    "Сделай bounded evergreen серию о рабочих процессах."
                )
                assert series.json()["cadence"]["weekday"] == 2

                invalid_extra = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-automation-invalid-0001",
                        "skill_id": "attention_today",
                        "skill_version": "1",
                        "operator_input": {"prompt": "publish everything"},
                        "cadence": {
                            "kind": "daily",
                            "local_time": "09:30",
                            "cron": "* * * * *",
                        },
                    },
                )
                assert invalid_extra.status_code == 422

                unsupported = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-automation-version-0001",
                        "skill_id": "attention_today",
                        "skill_version": "999",
                        "operator_input": {},
                        "cadence": {
                            "kind": "daily",
                            "local_time": "09:30",
                        },
                    },
                )
                assert unsupported.status_code == 422

                listed = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                )
                assert listed.status_code == 200
                assert {row["id"] for row in listed.json()} == {
                    body["id"],
                    series.json()["id"],
                }

                fetched = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{body['id']}",
                    headers=headers,
                )
                assert fetched.status_code == 200
                assert fetched.json()["definition_fingerprint"] == body["definition_fingerprint"]

                disabled = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{body['id']}/enabled",
                    headers=headers,
                    json={"enabled": False},
                )
                assert disabled.status_code == 200
                assert disabled.json()["enabled"] is False

                denied = await client.get(
                    f"/api/studio/channels/{foreign.id}/assistant/automations",
                    headers=headers,
                )
                assert denied.status_code == 404
                denied_direct = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/999999",
                    headers=headers,
                )
                assert denied_direct.status_code == 404

                forged_manual = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/runs",
                    headers=headers,
                    json={
                        "scenario": "attention_today",
                        "skill_id": "attention_today",
                        "skill_version": "1",
                    },
                )
                assert forged_manual.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(run())
