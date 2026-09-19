from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import AdminAgentApproval, AdminAgentRunArtifact
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.admin_agent import (
    PHASE_GENERATION_VALIDATED,
    PHASE_RESTART_REQUIRED,
    PHASE_SERIES_PERSISTED,
    SERIES_SCENARIO_LIMITS,
    AgentIdempotencyConflict,
    AdminAgentRunner,
    assistant_run_resume_state,
)
from app.services.ai_generation import AIGenerationService


def _payload(count: int = 4) -> str:
    return json.dumps(
        {
            "series": {
                "title": "Серия о понятных рабочих процессах",
                "summary": "Четыре самостоятельных evergreen материала с разными углами.",
            },
            "posts": [
                {
                    "title": f"Практический пост {index}",
                    "angle": f"Самостоятельный угол {index}",
                    "objective": f"Дать читателю применимый вывод {index}",
                    "text": f"Evergreen текст {index}: конкретный разбор без текущих новостей.",
                }
                for index in range(1, count + 1)
            ],
        },
        ensure_ascii=False,
    )


async def _setup_channel(session, tg_user_id: int = 12001):
    owner = await ClientsRepo(session).create_or_get(
        tg_user_id,
        f"series_{tg_user_id}",
        "Series Owner",
    )
    channel = await ChannelsRepo(session).create(
        owner.id,
        -2000000 - tg_user_id,
        "Series channel",
    )
    ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
    ai.enabled = True
    ai.model = "provider/model"
    await session.commit()
    return owner, channel


async def _count(session, model, *, channel_id: int | None = None) -> int:
    stmt = select(func.count(model.id))
    if channel_id is not None and hasattr(model, "channel_id"):
        stmt = stmt.where(model.channel_id == int(channel_id))
    return int((await session.execute(stmt)).scalar_one())


def test_content_series_success_is_atomic_idempotent_and_content_only(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session)
                calls: list[dict] = []

                async def generated(self, **kwargs):
                    calls.append(kwargs)
                    assert kwargs["channel_id"] == channel.id
                    assert "EDITORIAL_BRIEF" in kwargs["topic"]
                    assert "evergreen" in kwargs["topic"]
                    return {
                        "success": True,
                        "text": _payload(4),
                        "tokens_used": 44,
                        "model": "provider/model",
                        "error": None,
                    }

                monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
                runner = AdminAgentRunner(session, limits=SERIES_SCENARIO_LIMITS)
                result = await runner.run_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="series-request-0001",
                    brief="   Сделай evergreen серию про понятные рабочие процессы.   ",
                    post_count=4,
                )

                assert result.status == "completed"
                assert result.operator_input == {
                    "brief": "Сделай evergreen серию про понятные рабочие процессы.",
                    "post_count": 4,
                }
                assert result.result is not None
                assert result.result["scenario"] == "prepare_content_series"
                assert result.result["requested_post_count"] == 4
                assert len(result.result["posts"]) == 4
                assert [row["ordinal"] for row in result.result["posts"]] == [1, 2, 3, 4]
                assert all(row["status"] == "draft" for row in result.result["posts"])
                assert all("text" not in row for row in result.result["posts"])
                assert len(calls) == 1

                artifacts = list(
                    (
                        await session.execute(
                            select(AdminAgentRunArtifact)
                            .where(AdminAgentRunArtifact.run_id == result.id)
                            .order_by(AdminAgentRunArtifact.ordinal)
                        )
                    ).scalars()
                )
                assert len(artifacts) == 4
                assert [row.ordinal for row in artifacts] == [1, 2, 3, 4]
                assert {row.artifact_type for row in artifacts} == {"series_draft"}

                content_ids = [row.content_item_id for row in artifacts]
                items = list(
                    (
                        await session.execute(
                            select(ContentItem).where(ContentItem.id.in_(content_ids))
                        )
                    ).scalars()
                )
                revisions = list(
                    (
                        await session.execute(
                            select(ContentRevision).where(
                                ContentRevision.content_item_id.in_(content_ids)
                            )
                        )
                    ).scalars()
                )
                assert len(items) == len(revisions) == 4
                assert all(row.status == "draft" and row.kind == "post" for row in items)
                assert all(row.source == "admin_agent" for row in revisions)
                fingerprint = result.result["plan_fingerprint"]
                for item in items:
                    assert item.meta["admin_agent_run_id"] == result.id
                    assert item.meta["admin_agent_scenario"] == "prepare_content_series"
                    assert item.meta["skill_id"] == "prepare_content_series"
                    assert item.meta["skill_version"] == "1"
                    assert item.meta["plan_fingerprint"] == fingerprint
                    assert "brief" not in item.meta
                assert await _count(session, ScheduleEntry, channel_id=channel.id) == 0
                assert await _count(session, Publication, channel_id=channel.id) == 0
                assert await _count(session, AdminAgentApproval, channel_id=channel.id) == 0

                same = await runner.run_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="series-request-0001",
                    brief="Сделай evergreen серию про понятные рабочие процессы.",
                    post_count=4,
                )
                assert same.id == result.id
                assert len(calls) == 1
                with pytest.raises(AgentIdempotencyConflict):
                    await runner.run_prepare_content_series(
                        channel_id=channel.id,
                        owner_tg_user_id=owner.tg_user_id,
                        request_id="series-request-0001",
                        brief="Сделай evergreen серию про понятные рабочие процессы.",
                        post_count=3,
                    )
                assert len(calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "provider_text",
    [
        _payload(3),
        json.dumps(
            {
                "series": {"title": "Series", "summary": "Summary", "action": "publish"},
                "posts": [],
            }
        ),
        json.dumps(
            {
                "series": {"title": "Series", "summary": "Summary"},
                "posts": [
                    {
                        "title": "Same",
                        "angle": "Angle one",
                        "objective": "Objective one",
                        "text": "Same body",
                    },
                    {
                        "title": "Same",
                        "angle": "Angle two",
                        "objective": "Objective two",
                        "text": "Different body",
                    },
                    {
                        "title": "Three",
                        "angle": "Angle three",
                        "objective": "Objective three",
                        "text": "Third body",
                    },
                    {
                        "title": "Four",
                        "angle": "Angle four",
                        "objective": "Objective four",
                        "text": "Fourth body",
                    },
                ],
            }
        ),
    ],
)
def test_content_series_malformed_generation_fails_before_content_insert(
    monkeypatch,
    provider_text: str,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 12002)

                async def generated(self, **kwargs):
                    return {
                        "success": True,
                        "text": provider_text,
                        "tokens_used": 10,
                        "model": "provider/model",
                        "error": None,
                    }

                monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
                result = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).run_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="series-malformed-0001",
                    brief="Сделай четыре разных evergreen поста для канала.",
                    post_count=4,
                )
                assert result.status == "failed"
                assert await _count(session, ContentItem, channel_id=channel.id) == 0
                assert await _count(session, AdminAgentRunArtifact) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_content_series_validated_resume_does_not_call_llm_twice(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 12003)
                calls = 0

                async def generated(self, **kwargs):
                    nonlocal calls
                    calls += 1
                    return {
                        "success": True,
                        "text": _payload(4),
                        "tokens_used": 11,
                        "model": "provider/model",
                        "error": None,
                    }

                original = AdminAgentRunner._persist_validated_content_series

                async def interrupted(self, run):
                    raise RuntimeError("synthetic persistence interruption")

                monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_persist_validated_content_series",
                    interrupted,
                )
                first = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).run_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="series-resume-validated-0001",
                    brief="Сделай четыре разных evergreen поста для канала.",
                    post_count=4,
                )
                assert first.status == "failed"
                assert first.workflow_phase == PHASE_GENERATION_VALIDATED
                assert first.checkpoint is not None
                assert any("text" in post for post in first.checkpoint["posts"])
                assert calls == 1
                assert await _count(session, ContentItem, channel_id=channel.id) == 0

                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_persist_validated_content_series",
                    original,
                )
                resumed = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).resume_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    run_id=first.id,
                )
                assert resumed.status == "completed"
                assert len(resumed.result["posts"]) == 4
                assert calls == 1
                assert await _count(session, ContentItem, channel_id=channel.id) == 4
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_content_series_persisted_resume_reconstructs_and_partial_artifacts_fail_closed(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 12004)
                calls = 0

                async def generated(self, **kwargs):
                    nonlocal calls
                    calls += 1
                    return {
                        "success": True,
                        "text": _payload(4),
                        "tokens_used": 12,
                        "model": "provider/model",
                        "error": None,
                    }

                original_result = AdminAgentRunner._content_series_result_from_artifacts

                async def interrupted_result(self, run):
                    raise RuntimeError("synthetic completion interruption")

                monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_content_series_result_from_artifacts",
                    interrupted_result,
                )
                first = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).run_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="series-resume-persisted-0001",
                    brief="Сделай четыре разных evergreen поста для канала.",
                    post_count=4,
                )
                assert first.status == "failed"
                assert first.workflow_phase == PHASE_SERIES_PERSISTED
                assert first.checkpoint is not None
                assert all("text" not in post for post in first.checkpoint["posts"])
                before = await _count(session, ContentItem, channel_id=channel.id)
                assert before == 4
                assert calls == 1

                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_content_series_result_from_artifacts",
                    original_result,
                )
                resumed = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).resume_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    run_id=first.id,
                )
                assert resumed.status == "completed"
                assert calls == 1
                assert await _count(session, ContentItem, channel_id=channel.id) == before

                second = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).run_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="series-resume-partial-0001",
                    brief="Сделай четыре разных evergreen поста для канала.",
                    post_count=4,
                )
                assert second.status == "completed"
                run_id = second.id
                second.status = "failed"
                second.workflow_phase = PHASE_SERIES_PERSISTED
                second.checkpoint = {
                    "checkpoint_version": 1,
                    "state": PHASE_SERIES_PERSISTED,
                    "requested_post_count": 4,
                    "series": {
                        "title": second.result["series_title"],
                        "summary": second.result["series_summary"],
                    },
                    "posts": [
                        {
                            "ordinal": post["ordinal"],
                            "title": post["title"],
                            "angle": post["angle"],
                            "objective": post["objective"],
                        }
                        for post in second.result["posts"]
                    ],
                    "plan_fingerprint": second.result["plan_fingerprint"],
                    "context_summary": second.result["editorial_context"],
                }
                second.result = None
                await session.execute(
                    delete(AdminAgentRunArtifact).where(
                        AdminAgentRunArtifact.run_id == run_id,
                        AdminAgentRunArtifact.ordinal == 4,
                    )
                )
                await session.commit()
                failed_closed = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).resume_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    run_id=run_id,
                )
                assert failed_closed.status == "failed"
                assert failed_closed.workflow_phase == "failed_closed"
                assert calls == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_content_series_generation_inflight_is_restart_required_and_never_replayed(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 12005)
                calls = 0

                async def ambiguous(self, **kwargs):
                    nonlocal calls
                    calls += 1
                    raise RuntimeError("provider outcome unknown")

                monkeypatch.setattr(AIGenerationService, "run_pipeline", ambiguous)
                first = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).run_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="series-ambiguous-0001",
                    brief="Сделай четыре разных evergreen поста для канала.",
                    post_count=4,
                )
                assert first.status == "failed"
                assert first.workflow_phase == PHASE_RESTART_REQUIRED
                assert first.checkpoint is None
                assert assistant_run_resume_state(first)[1] == "restart_required"
                resumed = await AdminAgentRunner(
                    session,
                    limits=SERIES_SCENARIO_LIMITS,
                ).resume_prepare_content_series(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    run_id=first.id,
                )
                assert resumed.status == "failed"
                assert calls == 1
                assert await _count(session, ContentItem, channel_id=channel.id) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
