from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.studio.admin_agent as admin_agent_api_module
import app.api.studio.app as studio_app_module
from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig
from app.core.config import settings
from app.core.db import Base
from app.domain.admin_agent import AdminAgentAutomation, AdminAgentRun
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.admin_agent_automations import automation_definition_fingerprint


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
                assert body["disabled_reason"] is None
                assert body["health"] == "active"
                assert body["health_reason"] == "healthy"
                assert body["last_outcome"] is None
                assert body["usage_7d"]["tokens_used"] == 0
                assert body["usage_30d"]["occurrence_runs"] == 0
                assert body["execution_limits"]["max_llm_calls"] == 1
                assert body["cadence_occurrences_per_week"] == 7
                assert body["claim_active"] is False
                assert "claim_token" not in body
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
                disabled_body = disabled.json()
                assert disabled_body["enabled"] is False
                assert disabled_body["disabled_reason"] == "manual_pause"
                assert disabled_body["disabled_at"]
                assert disabled_body["health"] == "paused"
                assert disabled_body["health_reason"] == "manual_pause"

                enabled = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{body['id']}/enabled",
                    headers=headers,
                    json={"enabled": True},
                )
                assert enabled.status_code == 200
                assert enabled.json()["enabled"] is True
                assert enabled.json()["disabled_reason"] is None
                assert enabled.json()["health"] == "active"

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



def test_studio_automation_e5_history_and_unsafe_enable_are_fail_closed(monkeypatch) -> None:
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
                    17101, "owner_e5", "Owner E5"
                )
                channel = await ChannelsRepo(session).create(
                    owner.id, -10017101, "Owned E5"
                )
                other = await ClientsRepo(session).create_or_get(
                    17102, "other_e5", "Other E5"
                )
                foreign = await ChannelsRepo(session).create(
                    other.id, -10017102, "Foreign E5"
                )

            headers = {"X-Telegram-Init-Data": _init_data(17101)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://studio",
            ) as client:
                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-e5-history-0001",
                        "skill_id": "drafts_tomorrow",
                        "skill_version": "1",
                        "operator_input": {},
                        "cadence": {"kind": "daily", "local_time": "09:30"},
                    },
                )
                assert created.status_code == 201
                automation_id = int(created.json()["id"])

                now = datetime.now(timezone.utc).replace(microsecond=0)
                async with Session() as session:
                    session.add_all(
                        [
                            AdminAgentRun(
                                owner_tg_user_id=17101,
                                channel_id=channel.id,
                                scenario="drafts_tomorrow",
                                request_id="studio-e5-run-owned-1",
                                operator_input={},
                                skill_id="drafts_tomorrow",
                                skill_version="1",
                                automation_id=automation_id,
                                scheduled_for=now,
                                workflow_phase="restart_required",
                                checkpoint={"secret": "must-not-leak"},
                                status="failed",
                                tokens_used=21,
                                model="provider/model",
                                result={"scenario": "drafts_tomorrow", "draft_count": 3},
                                error="internal failure detail",
                            ),
                            AdminAgentRun(
                                owner_tg_user_id=17102,
                                channel_id=channel.id,
                                scenario="drafts_tomorrow",
                                request_id="studio-e5-run-foreign-owner",
                                operator_input={},
                                skill_id="drafts_tomorrow",
                                skill_version="1",
                                automation_id=automation_id,
                                scheduled_for=now + timedelta(seconds=1),
                                workflow_phase="completed",
                                checkpoint={"foreign": True},
                                status="completed",
                                tokens_used=999,
                            ),
                        ]
                    )
                    await session.commit()

                history = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}/runs?limit=1",
                    headers=headers,
                )
                assert history.status_code == 200
                rows = history.json()
                assert len(rows) == 1
                assert rows[0]["id"]
                assert rows[0]["workflow_phase"] == "restart_required"
                assert rows[0]["resume_state"] == "restart_required"
                assert rows[0]["tokens_used"] == 21
                assert rows[0]["result_metadata"] == {"draft_count": 3}
                assert "checkpoint" not in rows[0]
                assert "error" not in rows[0]
                assert "result" not in rows[0]

                bounded = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}/runs?limit=51",
                    headers=headers,
                )
                assert bounded.status_code == 422

                status_view = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}",
                    headers=headers,
                )
                assert status_view.status_code == 200
                status_body = status_view.json()
                assert status_body["health"] == "needs_attention"
                assert status_body["health_reason"] == "restart_required"
                assert status_body["latest_run"]["tokens_used"] == 21
                assert status_body["usage_7d"]["tokens_used"] == 21
                assert "claim_token" not in status_body

                paused = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}/enabled",
                    headers=headers,
                    json={"enabled": False},
                )
                assert paused.status_code == 200

                async with Session() as session:
                    row = await session.scalar(
                        select(AdminAgentAutomation).where(
                            AdminAgentAutomation.id == automation_id
                        )
                    )
                    assert row is not None
                    row.skill_version = "999"
                    row.definition_fingerprint = automation_definition_fingerprint(
                        skill_id=str(row.skill_id),
                        skill_version=str(row.skill_version),
                        operator_input=dict(row.operator_input or {}),
                        cadence_kind=str(row.cadence_kind),
                        local_time_value=str(row.local_time),
                        weekday=row.weekday,
                        timezone_name=str(row.timezone),
                    )
                    await session.commit()

                rejected = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}/enabled",
                    headers=headers,
                    json={"enabled": True},
                )
                assert rejected.status_code == 409
                assert rejected.json()["detail"] == "unsupported_skill_version"

                blocked = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}",
                    headers=headers,
                )
                assert blocked.status_code == 200
                blocked_body = blocked.json()
                assert blocked_body["enabled"] is False
                assert blocked_body["health"] == "blocked"
                assert blocked_body["disabled_reason"] == "unsupported_skill_version"
                assert blocked_body["migration_available"] is True
                assert blocked_body["suggested_skill_id"] == "drafts_tomorrow"
                assert blocked_body["suggested_skill_version"] == "1"

                foreign_history = await client.get(
                    f"/api/studio/channels/{foreign.id}/assistant/automations/{automation_id}/runs",
                    headers=headers,
                )
                assert foreign_history.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_studio_automation_history_cursor_is_deterministic(monkeypatch) -> None:
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
                    17201, "history_owner", "History Owner"
                )
                channel = await ChannelsRepo(session).create(
                    owner.id, -10017201, "Automation History"
                )

            headers = {"X-Telegram-Init-Data": _init_data(17201)}
            app = create_studio_app(_config())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://studio",
            ) as client:
                created = await client.post(
                    f"/api/studio/channels/{channel.id}/assistant/automations",
                    headers=headers,
                    json={
                        "request_id": "studio-history-cursor-0001",
                        "skill_id": "drafts_tomorrow",
                        "skill_version": "1",
                        "operator_input": {},
                        "cadence": {"kind": "daily", "local_time": "09:30"},
                    },
                )
                assert created.status_code == 201
                automation_id = int(created.json()["id"])

                first_scheduled_for = datetime(
                    2026, 9, 19, 9, 30, tzinfo=timezone.utc
                )
                async with Session() as session:
                    rows = []
                    for index in range(4):
                        row = AdminAgentRun(
                            owner_tg_user_id=17201,
                            channel_id=channel.id,
                            scenario="drafts_tomorrow",
                            request_id=f"studio-history-run-{index}",
                            operator_input={},
                            skill_id="drafts_tomorrow",
                            skill_version="1",
                            automation_id=automation_id,
                            scheduled_for=first_scheduled_for + timedelta(minutes=index),
                            workflow_phase="completed",
                            checkpoint={},
                            status="completed",
                            tokens_used=index,
                        )
                        session.add(row)
                        rows.append(row)
                    await session.commit()
                    expected_ids = [int(row.id) for row in reversed(rows)]

                first = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}/runs",
                    headers=headers,
                    params={"limit": 2},
                )
                assert first.status_code == 200
                first_rows = first.json()
                assert [row["id"] for row in first_rows] == expected_ids[:2]

                cursor = first_rows[-1]
                second = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}/runs",
                    headers=headers,
                    params={
                        "limit": 2,
                        "before_scheduled_for": cursor["scheduled_for"],
                        "before_id": cursor["id"],
                    },
                )
                assert second.status_code == 200
                assert [row["id"] for row in second.json()] == expected_ids[2:]

                incomplete = await client.get(
                    f"/api/studio/channels/{channel.id}/assistant/automations/{automation_id}/runs",
                    headers=headers,
                    params={"before_id": expected_ids[0]},
                )
                assert incomplete.status_code == 422
                assert "provided together" in incomplete.json()["detail"]
        finally:
            await engine.dispose()

    asyncio.run(run())
