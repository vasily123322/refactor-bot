from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import AdminAgentEvent
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo
from app.services.admin_agent import (
    ATTENTION_TOOLS,
    DRAFT_SCENARIO_LIMITS,
    SIDE_EFFECT_DRAFT_WRITE,
    AdminAgentRunner,
    AgentLimits,
    AgentToolSpec,
    BoundedToolRegistry,
)
from app.services.ai_generation import AIGenerationService


def _generated_payload(*, suffix: str = "") -> str:
    return json.dumps(
        {
            "drafts": [
                {
                    "title": f"Практический разбор{suffix}",
                    "text": "Разберите один рабочий процесс и покажите три шага, которые можно применить без привязки к новостям.",
                },
                {
                    "title": f"Вопрос подписчика{suffix}",
                    "text": "Начните с типичного вопроса аудитории, дайте короткий ответ и предложите проверить подход на собственном примере.",
                },
                {
                    "title": f"Чек-лист на день{suffix}",
                    "text": "Соберите компактный evergreen чек-лист из нескольких пунктов и завершите нейтральным приглашением сохранить его.",
                },
            ]
        },
        ensure_ascii=False,
    )


async def _setup_channel(session, *, tg_user_id: int = 9801):
    owner = await ClientsRepo(session).create_or_get(tg_user_id, f"owner_{tg_user_id}", "Owner")
    channel = await ChannelsRepo(session).create(owner.id, -1000000 - tg_user_id, "Owned")
    ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
    ai.enabled = True
    ai.model = "provider/model"
    await session.commit()
    return owner, channel, ai


async def _content_count(session, channel_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count(ContentItem.id)).where(ContentItem.channel_id == int(channel_id))
            )
        ).scalar_one()
    )


def test_draft_write_is_not_admitted_to_read_only_tool_registry() -> None:
    async def write_tool(_ctx):
        return {}

    with pytest.raises(ValueError, match="read-only"):
        BoundedToolRegistry(
            (AgentToolSpec("draft_batch", SIDE_EFFECT_DRAFT_WRITE, write_tool),)
        )
    assert ATTENTION_TOOLS.names == (
        "schedule_attention",
        "publication_attention",
        "source_health",
        "ai_health",
    )


def test_drafts_tomorrow_success_is_atomic_content_domain_only_and_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel, _ = await _setup_channel(session)
                # At 07:00 UTC it is exactly midnight in Los Angeles (PDT), so
                # tomorrow for the channel is 2026-09-20, not derived from server locale.
                now = datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc)
                schedule = ScheduleEntry(
                    content_item_id=999001,
                    content_revision=1,
                    channel_id=channel.id,
                    scheduled_at=now + timedelta(hours=2),
                    timezone="America/Los_Angeles",
                    status="pending",
                    repeat_rule={},
                    meta={},
                )
                session.add(schedule)
                await session.commit()

                calls: list[dict] = []

                async def generated(self, **kwargs):
                    calls.append(kwargs)
                    return {
                        "success": True,
                        "text": _generated_payload(),
                        "tokens_used": 321,
                        "model": "provider/model",
                        "error": None,
                    }

                monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)

                runner = AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                    now_utc=now,
                )
                result = await runner.run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-request-0001",
                )
                assert result.status == "completed"
                assert result.request_id == "draft-request-0001"
                assert result.result is not None
                assert result.result["scenario"] == "drafts_tomorrow"
                assert result.result["timezone"] == "America/Los_Angeles"
                assert result.result["target_local_date"] == "2026-09-20"
                assert result.result["draft_count"] == 3
                assert result.result["write_capability"] == "draft_write"
                assert len(result.result["drafts"]) == 3
                assert len(calls) == 1
                assert calls[0]["channel_id"] == channel.id
                assert calls[0]["mode"] == "from_scratch"
                assert calls[0].get("user_id") is None
                assert calls[0].get("prompt_key") is None

                items = list(
                    (
                        await session.execute(
                            select(ContentItem)
                            .where(ContentItem.channel_id == channel.id)
                            .order_by(ContentItem.id)
                        )
                    ).scalars()
                )
                assert len(items) == 3
                assert all(item.status == "draft" for item in items)
                assert all(item.current_revision == 1 for item in items)
                assert all(item.channel_id == channel.id for item in items)
                assert len({item.title for item in items}) == 3

                revisions = list(
                    (
                        await session.execute(
                            select(ContentRevision)
                            .where(ContentRevision.content_item_id.in_([item.id for item in items]))
                            .order_by(ContentRevision.content_item_id)
                        )
                    ).scalars()
                )
                assert len(revisions) == 3
                assert all(row.source == "admin_agent" for row in revisions)
                assert all(row.created_by_tg_user_id == owner.tg_user_id for row in revisions)
                assert all(PostDocument.from_dict(row.document).primary_text() for row in revisions)
                for index, (item, revision) in enumerate(zip(items, revisions, strict=True), start=1):
                    expected = {
                        "admin_agent_run_id": result.id,
                        "admin_agent_scenario": "drafts_tomorrow",
                        "target_local_date": "2026-09-20",
                        "draft_index": index,
                    }
                    assert item.meta == expected
                    assert revision.meta == expected
                    assert PostDocument.from_dict(revision.document).metadata == expected

                assert (
                    await session.execute(select(func.count(ScheduleEntry.id)))
                ).scalar_one() == 1
                assert (
                    await session.execute(select(func.count(Publication.id)))
                ).scalar_one() == 0

                events = list(
                    (
                        await session.execute(
                            select(AdminAgentEvent)
                            .where(AdminAgentEvent.run_id == result.id)
                            .order_by(AdminAgentEvent.sequence)
                        )
                    ).scalars()
                )
                event_types = [event.event_type for event in events]
                assert "generation_started" in event_types
                assert "generation_finished" in event_types
                assert "draft_batch_started" in event_types
                assert "draft_batch_finished" in event_types
                assert events[-1].event_type == "run_completed"
                serialized_events = repr([(event.event_type, event.payload) for event in events])
                assert "Практический разбор" not in serialized_events
                assert "Разберите один рабочий процесс" not in serialized_events

                retry = await runner.run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-request-0001",
                )
                assert retry.id == result.id
                assert retry.result == result.result
                assert await _content_count(session, channel.id) == 3
                assert len(calls) == 1

                second = await runner.run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-request-0002",
                )
                assert second.id != result.id
                assert second.status == "completed"
                assert await _content_count(session, channel.id) == 6
                assert len(calls) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("provider_result", "expected_calls"),
    [
        (
            {
                "success": False,
                "text": None,
                "tokens_used": 0,
                "error": "provider failed with secret payload",
            },
            1,
        ),
        (
            {
                "success": True,
                "text": "not-json",
                "tokens_used": 1,
                "error": None,
            },
            1,
        ),
        (
            {
                "success": True,
                "text": json.dumps({"drafts": [{"title": "a", "text": "b"}]}),
                "tokens_used": 1,
                "error": None,
            },
            1,
        ),
        (
            {
                "success": True,
                "text": json.dumps(
                    {
                        "drafts": [
                            {"title": "A", "text": "same"},
                            {"title": "B", "text": "same"},
                            {"title": "C", "text": "different"},
                        ]
                    }
                ),
                "tokens_used": 1,
                "error": None,
            },
            1,
        ),
    ],
)
def test_draft_generation_failures_create_no_partial_content(
    monkeypatch,
    provider_result,
    expected_calls,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel, _ = await _setup_channel(session, tg_user_id=9810)
                calls = 0

                async def provider(self, **kwargs):
                    nonlocal calls
                    calls += 1
                    return provider_result

                monkeypatch.setattr(AIGenerationService, "run_pipeline", provider)
                result = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-failure-0001",
                )
                assert result.status == "failed"
                assert result.result is None
                assert result.error == "admin agent execution failed"
                assert await _content_count(session, channel.id) == 0
                assert calls == expected_calls
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
                assert "provider failed with secret payload" not in serialized
                assert events[-1].event_type == "run_failed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_drafts_tomorrow_ai_disabled_and_quota_exhausted_fail_without_content() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel, ai = await _setup_channel(session, tg_user_id=9820)
                ai.enabled = False
                await session.commit()
                disabled = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-disabled-0001",
                )
                assert disabled.status == "failed"
                assert await _content_count(session, channel.id) == 0

            async with Session() as session:
                owner, channel, ai = await _setup_channel(session, tg_user_id=9821)
                ai.enabled = True
                ai.tokens_limit_day = 10
                ai.tokens_used_day = 10
                await session.commit()
                exhausted = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-quota-0001",
                )
                assert exhausted.status == "failed"
                assert await _content_count(session, channel.id) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_drafts_tomorrow_timeout_and_persistence_failure_create_no_content(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                owner, channel, _ = await _setup_channel(session, tg_user_id=9830)

                async def slow_provider(self, **kwargs):
                    await asyncio.sleep(0.05)
                    return {
                        "success": True,
                        "text": _generated_payload(),
                        "tokens_used": 1,
                        "error": None,
                    }

                monkeypatch.setattr(AIGenerationService, "run_pipeline", slow_provider)
                timeout = await AdminAgentRunner(
                    session,
                    limits=AgentLimits(
                        max_steps=4,
                        max_tool_calls=0,
                        max_llm_calls=1,
                        max_seconds=0.001,
                        max_items_per_tool=0,
                    ),
                ).run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-timeout-0001",
                )
                assert timeout.status == "failed"
                assert timeout.error == "admin agent wall-clock limit exceeded"
                assert await _content_count(session, channel.id) == 0

            async with Session() as session:
                owner, channel, _ = await _setup_channel(session, tg_user_id=9831)

                async def provider(self, **kwargs):
                    return {
                        "success": True,
                        "text": _generated_payload("-persistence"),
                        "tokens_used": 1,
                        "error": None,
                    }

                async def fail_batch(self, **kwargs):
                    raise RuntimeError("database write failed")

                monkeypatch.setattr(AIGenerationService, "run_pipeline", provider)
                monkeypatch.setattr(ContentRepo, "create_batch", fail_batch)
                failed = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="draft-persist-0001",
                )
                assert failed.status == "failed"
                assert await _content_count(session, channel.id) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_content_batch_rolls_back_when_second_flush_fails(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel, _ = await _setup_channel(session, tg_user_id=9840)
                original_flush = session.flush
                calls = 0

                async def flaky_flush(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("synthetic second draft persistence failure")
                    return await original_flush(*args, **kwargs)

                monkeypatch.setattr(session, "flush", flaky_flush)
                with pytest.raises(RuntimeError, match="second draft"):
                    await ContentRepo(session).create_batch(
                        channel_id=channel.id,
                        items=[
                            {
                                "title": f"draft {index}",
                                "document": PostDocument(
                                    blocks=[
                                        {
                                            "id": f"draft-{index}",
                                            "type": "text",
                                            "text": f"text {index}",
                                        }
                                    ]
                                ),
                                "metadata": {"index": index},
                            }
                            for index in range(1, 4)
                        ],
                        status="draft",
                        source="admin_agent",
                        created_by_tg_user_id=owner.tg_user_id,
                    )

                # Restore flush before issuing SELECT, then prove the transaction left no row.
                monkeypatch.setattr(session, "flush", original_flush)
                assert await _content_count(session, channel.id) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
