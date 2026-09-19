from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import AdminAgentEvent, AdminAgentRun
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.sources_v2 import SourcesRepo
from app.services.admin_agent import (
    ATTENTION_TOOLS,
    AdminAgentRunner,
    AgentLimits,
    SIDE_EFFECT_READ_ONLY,
)
from app.services.ai_generation import AIGenerationService


def test_admin_agent_registry_is_allowlisted_read_only() -> None:
    assert ATTENTION_TOOLS.names == (
        "schedule_attention",
        "publication_attention",
        "source_health",
        "ai_health",
    )
    assert all(
        ATTENTION_TOOLS.get(name).side_effect == SIDE_EFFECT_READ_ONLY
        for name in ATTENTION_TOOLS.names
    )


def test_attention_run_is_channel_scoped_persistent_and_timezone_aware(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner = await ClientsRepo(session).create_or_get(9101, "owner", "Owner")
                channel = await ChannelsRepo(session).create(owner.id, -1009101, "Owned")
                foreign = await ChannelsRepo(session).create(owner.id, -1009102, "Foreign")

                ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
                ai.enabled = False
                await session.commit()

                # 2026-09-19 07:00 UTC is midnight in America/Los_Angeles (PDT).
                now = datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc)
                overdue = ScheduleEntry(
                    content_item_id=101,
                    content_revision=1,
                    channel_id=channel.id,
                    scheduled_at=now - timedelta(minutes=1),
                    timezone="America/Los_Angeles",
                    status="pending",
                    repeat_rule={},
                    meta={},
                )
                today = ScheduleEntry(
                    content_item_id=102,
                    content_revision=1,
                    channel_id=channel.id,
                    scheduled_at=now + timedelta(minutes=30),
                    timezone="America/Los_Angeles",
                    status="pending",
                    repeat_rule={},
                    meta={},
                )
                foreign_schedule = ScheduleEntry(
                    content_item_id=999,
                    content_revision=1,
                    channel_id=foreign.id,
                    scheduled_at=now - timedelta(hours=2),
                    timezone="UTC",
                    status="pending",
                    repeat_rule={},
                    meta={},
                )
                session.add_all([overdue, today, foreign_schedule])
                await session.flush()
                publication = Publication(
                    schedule_entry_id=overdue.id,
                    content_item_id=101,
                    content_revision=1,
                    channel_id=channel.id,
                    status="failed",
                    last_error="delivery failed",
                    execution_mode="canonical",
                    attempt_count=1,
                    meta={},
                )
                foreign_publication = Publication(
                    schedule_entry_id=foreign_schedule.id,
                    content_item_id=999,
                    content_revision=1,
                    channel_id=foreign.id,
                    status="failed",
                    last_error="foreign failure",
                    execution_mode="canonical",
                    attempt_count=1,
                    meta={},
                )
                session.add_all([publication, foreign_publication])
                source = await SourcesRepo(session).create_connector(
                    channel_id=channel.id,
                    kind="rss",
                    value="https://example.com/owned.xml",
                )
                source.status = "degraded"
                source.status_reason = "fetch failures"
                foreign_source = await SourcesRepo(session).create_connector(
                    channel_id=foreign.id,
                    kind="rss",
                    value="https://example.com/foreign.xml",
                )
                foreign_source.status = "broken"
                await session.commit()

                async def forbidden_provider_call(*args, **kwargs):
                    raise AssertionError("AI provider must not run while Channel AI is disabled")

                monkeypatch.setattr(AIGenerationService, "run_pipeline", forbidden_provider_call)

                result = await AdminAgentRunner(session, now_utc=now).run_attention_today(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                )
                assert result.status == "completed"
                assert result.result is not None
                assert result.result["timezone"] == "America/Los_Angeles"
                assert result.result["generated_by"] == "deterministic_fallback"

                items = result.result["attention_items"]
                fact_ids = {item["fact_id"] for item in items}
                assert f"schedule:{overdue.id}:overdue" in fact_ids
                assert f"schedule:{today.id}:today" in fact_ids
                assert f"publication:{publication.id}:failure" in fact_ids
                assert f"source:{source.id}:degraded" in fact_ids
                assert "ai:disabled" in fact_ids
                serialized = repr(items)
                assert all(
                    item.get("refs", {}).get("schedule_entry_id") != foreign_schedule.id
                    for item in items
                )
                assert "foreign failure" not in serialized
                assert "foreign.xml" not in serialized

                persisted = await session.get(AdminAgentRun, result.id)
                assert persisted is not None
                events = list(
                    (
                        await session.execute(
                            select(AdminAgentEvent)
                            .where(AdminAgentEvent.run_id == result.id)
                            .order_by(AdminAgentEvent.sequence)
                        )
                    ).scalars()
                )
                assert events[0].event_type == "run_started"
                assert events[-1].event_type == "run_completed"
                assert sum(event.event_type == "tool_started" for event in events) == 4
                assert sum(event.event_type == "tool_finished" for event in events) == 4

                refreshed_overdue = await session.get(ScheduleEntry, overdue.id)
                refreshed_publication = await session.get(Publication, publication.id)
                assert refreshed_overdue.status == "pending"
                assert refreshed_publication.status == "failed"
                assert refreshed_publication.last_error == "delivery failed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_admin_agent_hard_limit_records_deterministic_failure() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                runner = AdminAgentRunner(
                    session,
                    limits=AgentLimits(max_steps=5, max_tool_calls=1, max_llm_calls=1, max_seconds=5),
                )
                result = await runner.run_attention_today(channel_id=77, owner_tg_user_id=8800)
                assert result.status == "failed"
                assert result.error == "admin agent tool-call limit exceeded"
                events = list(
                    (
                        await session.execute(
                            select(AdminAgentEvent)
                            .where(AdminAgentEvent.run_id == result.id)
                            .order_by(AdminAgentEvent.sequence)
                        )
                    ).scalars()
                )
                assert events[-1].event_type == "run_failed"
                assert events[-1].payload == {"reason": "execution_limit"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_admin_agent_ai_disabled_and_exhausted_quota_do_not_bypass_provider(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            calls = 0

            async def provider(*args, **kwargs):
                nonlocal calls
                calls += 1
                return {"success": True, "text": "should not run", "tokens_used": 1}

            monkeypatch.setattr(AIGenerationService, "run_pipeline", provider)

            async with Session() as session:
                ai = await ChannelAISettingsRepo(session).get_or_create(501)
                ai.enabled = False
                await session.commit()
                disabled = await AdminAgentRunner(session).run_attention_today(
                    channel_id=501,
                    owner_tg_user_id=9501,
                )
                assert disabled.status == "completed"
                assert any(
                    item["fact_id"] == "ai:disabled"
                    for item in disabled.result["attention_items"]
                )

                quota = await ChannelAISettingsRepo(session).get_or_create(502)
                quota.enabled = True
                quota.tokens_limit_day = 100
                quota.tokens_used_day = 100
                await session.commit()
                exhausted = await AdminAgentRunner(session).run_attention_today(
                    channel_id=502,
                    owner_tg_user_id=9502,
                )
                assert exhausted.status == "completed"
                assert any(
                    item["fact_id"] == "ai:quota:day"
                    for item in exhausted.result["attention_items"]
                )
                assert calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_admin_agent_provider_failure_is_durable_and_does_not_store_prompt(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async def provider_failure(*args, **kwargs):
                return {
                    "success": False,
                    "text": None,
                    "tokens_used": 0,
                    "error": "provider secret payload must not be persisted",
                }

            monkeypatch.setattr(AIGenerationService, "run_pipeline", provider_failure)
            async with Session() as session:
                ai = await ChannelAISettingsRepo(session).get_or_create(503)
                ai.enabled = True
                await session.commit()
                source = await SourcesRepo(session).create_connector(
                    channel_id=503,
                    kind="rss",
                    value="https://example.com/provider-failure.xml",
                )
                source.status = "degraded"
                await session.commit()

                result = await AdminAgentRunner(session).run_attention_today(
                    channel_id=503,
                    owner_tg_user_id=9503,
                )
                assert result.status == "failed"
                assert result.error == "admin agent execution failed"
                events = list(
                    (
                        await session.execute(
                            select(AdminAgentEvent)
                            .where(AdminAgentEvent.run_id == result.id)
                            .order_by(AdminAgentEvent.sequence)
                        )
                    ).scalars()
                )
                serialized = repr([(event.event_type, event.payload) for event in events])
                assert "provider secret payload" not in serialized
                assert "FACTS_JSON" not in serialized
                assert events[-1].event_type == "run_failed"
        finally:
            await engine.dispose()

    asyncio.run(run())
