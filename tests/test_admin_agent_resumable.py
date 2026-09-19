from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import AdminAgentRun, AdminAgentRunArtifact
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo
from app.services.admin_agent import (
    DRAFT_SCENARIO_LIMITS,
    PHASE_DRAFTS_PERSISTED,
    PHASE_FAILED_CLOSED,
    PHASE_GENERATION_VALIDATED,
    AgentExecutionBusy,
    AgentResumeError,
    AdminAgentRunner,
    assistant_run_resume_state,
)
from app.services.admin_agent_context import (
    MAX_CONTEXT_EXCERPT_CHARS,
    MAX_CONTEXT_ITEMS,
    MAX_CONTEXT_TOTAL_CHARS,
    EditorialContextService,
)
from app.services.admin_agent_skills import (
    CAPABILITY_DRAFT_WRITE,
    CAPABILITY_READ_ONLY,
    CONTEXT_EDITORIAL_V1,
    RESUME_EXPLICIT,
    RESUME_NONE,
    AdminAgentSkillRegistry,
    AdminAgentSkillSpec,
    SKILL_REGISTRY,
)
from app.services.ai_generation import AIGenerationService


def _drafts() -> list[dict[str, str]]:
    return [
        {"title": "One", "text": "Evergreen one"},
        {"title": "Two", "text": "Evergreen two"},
        {"title": "Three", "text": "Evergreen three"},
    ]


def _generated_payload() -> str:
    return json.dumps({"drafts": _drafts()}, ensure_ascii=False)


async def _setup_channel(session, *, tg_user_id: int):
    owner = await ClientsRepo(session).create_or_get(tg_user_id, f"owner_{tg_user_id}", "Owner")
    channel = await ChannelsRepo(session).create(owner.id, -1000000 - tg_user_id, "Owned")
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


def test_skill_registry_is_immutable_versioned_and_fails_closed() -> None:
    attention = SKILL_REGISTRY.current_for_scenario("attention_today")
    drafts = SKILL_REGISTRY.current_for_scenario("drafts_tomorrow")
    assert (attention.skill_id, attention.version, attention.resume_policy) == (
        "attention_today",
        "1",
        RESUME_NONE,
    )
    assert attention.allowed_capability_classes == (CAPABILITY_READ_ONLY,)
    assert (drafts.skill_id, drafts.version, drafts.resume_policy) == (
        "drafts_tomorrow",
        "1",
        RESUME_EXPLICIT,
    )
    assert drafts.allowed_capability_classes == (CAPABILITY_DRAFT_WRITE,)
    assert drafts.context_profile == CONTEXT_EDITORIAL_V1
    with pytest.raises(TypeError):
        drafts.execution_limits["max_steps"] = 99

    with pytest.raises(KeyError, match="unknown admin-agent skill version"):
        SKILL_REGISTRY.resolve("drafts_tomorrow", "999")

    with pytest.raises(ValueError, match="unexpected capability"):
        AdminAgentSkillRegistry(
            (
                AdminAgentSkillSpec(
                    skill_id="bad-attention",
                    version="1",
                    scenario="attention_today",
                    execution_limits={"max_steps": 1},
                    allowed_capability_classes=(CAPABILITY_DRAFT_WRITE,),
                    context_profile="none",
                    resume_policy=RESUME_NONE,
                ),
            )
        )


def test_editorial_context_is_channel_scoped_and_bounded() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner1, channel1 = await _setup_channel(session, tg_user_id=9911)
                owner2, channel2 = await _setup_channel(session, tg_user_id=9912)
                repo = ContentRepo(session)
                first_item = None
                for index in range(10):
                    item = await repo.create(
                        channel_id=channel1.id,
                        title=f"channel-one-{index}",
                        document=PostDocument(
                            blocks=[
                                {
                                    "id": f"one-{index}",
                                    "type": "text",
                                    "text": ("A" * 600) + f" channel-one-body-{index}",
                                }
                            ]
                        ),
                        created_by_tg_user_id=owner1.tg_user_id,
                    )
                    if first_item is None:
                        first_item = item
                await repo.create(
                    channel_id=channel2.id,
                    title="other-channel-secret",
                    document=PostDocument(
                        blocks=[
                            {
                                "id": "other",
                                "type": "text",
                                "text": "OTHER_CHANNEL_BODY_SHOULD_NEVER_APPEAR",
                            }
                        ]
                    ),
                    created_by_tg_user_id=owner2.tg_user_id,
                )
                assert first_item is not None
                session.add(
                    ScheduleEntry(
                        content_item_id=int(first_item.id),
                        content_revision=1,
                        channel_id=int(channel1.id),
                        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=4),
                        timezone="UTC",
                        status="pending",
                        repeat_rule={},
                        meta={},
                    )
                )
                await session.commit()

                snapshot = await EditorialContextService(session).snapshot(
                    channel_id=channel1.id,
                    now_utc=datetime.now(timezone.utc),
                )
                payload = snapshot.prompt_payload()
                serialized = json.dumps(payload, ensure_ascii=False)
                assert len(snapshot.items) <= MAX_CONTEXT_ITEMS
                assert snapshot.total_excerpt_chars <= MAX_CONTEXT_TOTAL_CHARS
                assert all(
                    len(str(item.get("excerpt") or "")) <= MAX_CONTEXT_EXCERPT_CHARS
                    for item in snapshot.items
                )
                assert "other-channel-secret" not in serialized
                assert "OTHER_CHANNEL_BODY_SHOULD_NEVER_APPEAR" not in serialized
                assert all("source_body" not in item for item in snapshot.items)
                assert all("content_item_id" in item and "revision" in item for item in snapshot.items)
                assert snapshot.fingerprint
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_editorial_context_keeps_promptbuilder_memory_and_profile_contracts() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _owner, channel = await _setup_channel(session, tg_user_id=9915)
                ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
                ai.tone = "expert"
                ai.filters = {
                    "memory": {
                        "brand": "D_MEMORY_BRAND",
                        "style": "D_MEMORY_STYLE",
                    },
                    "publication_profile": "analysis",
                }
                await session.commit()

                _settings, _model, system_prompt, user_prompt = await AIGenerationService(
                    session
                ).build_prompt(
                    channel.id,
                    mode="from_scratch",
                    topic="EDITORIAL_CONTEXT_JSON:{\"items\":[]}",
                    extra={
                        "force_custom": False,
                        "date": "2026-09-20",
                        "schedule": "",
                    },
                )
                assert "D_MEMORY_BRAND" in system_prompt
                assert "D_MEMORY_STYLE" in system_prompt
                assert "Профиль публикации — Разбор" in system_prompt
                assert "EDITORIAL_CONTEXT_JSON" in user_prompt
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_resume_after_validated_checkpoint_does_not_call_llm_twice(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9921)
                provider_calls = 0

                async def provider(self, **kwargs):
                    nonlocal provider_calls
                    provider_calls += 1
                    return {
                        "success": True,
                        "text": _generated_payload(),
                        "tokens_used": 7,
                        "model": "provider/model",
                        "error": None,
                    }

                original_persist = AdminAgentRunner._persist_validated_drafts

                async def crash_after_checkpoint(self, run):
                    raise RuntimeError("synthetic process interruption")

                monkeypatch.setattr(AIGenerationService, "run_pipeline", provider)
                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_persist_validated_drafts",
                    crash_after_checkpoint,
                )
                runner = AdminAgentRunner(session, limits=DRAFT_SCENARIO_LIMITS)
                interrupted = await runner.run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="resume-checkpoint-0001",
                )
                assert interrupted.status == "failed"
                assert interrupted.skill_id == "drafts_tomorrow"
                assert interrupted.skill_version == "1"
                assert interrupted.workflow_phase == PHASE_GENERATION_VALIDATED
                assert interrupted.checkpoint is not None
                assert len(interrupted.checkpoint["drafts"]) == 3
                assert assistant_run_resume_state(interrupted)[0] is True
                assert provider_calls == 1
                assert await _count(session, ContentItem, channel_id=channel.id) == 0

                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_persist_validated_drafts",
                    original_persist,
                )
                resumed = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).resume_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    run_id=interrupted.id,
                )
                assert resumed.status == "completed"
                assert resumed.checkpoint is None
                assert provider_calls == 1
                assert await _count(session, ContentItem, channel_id=channel.id) == 3
                assert await _count(session, AdminAgentRunArtifact) == 3
                assert await _count(session, ScheduleEntry, channel_id=channel.id) == 0
                assert await _count(session, Publication, channel_id=channel.id) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_resume_after_atomic_persistence_reconstructs_exact_artifacts(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9931)
                provider_calls = 0

                async def provider(self, **kwargs):
                    nonlocal provider_calls
                    provider_calls += 1
                    return {
                        "success": True,
                        "text": _generated_payload(),
                        "tokens_used": 9,
                        "model": "provider/model",
                        "error": None,
                    }

                original_result = AdminAgentRunner._result_from_artifacts

                async def crash_after_persist(self, run):
                    raise RuntimeError("synthetic finalize interruption")

                monkeypatch.setattr(AIGenerationService, "run_pipeline", provider)
                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_result_from_artifacts",
                    crash_after_persist,
                )
                interrupted = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).run_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    request_id="resume-persisted-0001",
                )
                assert interrupted.status == "failed"
                assert interrupted.workflow_phase == PHASE_DRAFTS_PERSISTED
                assert interrupted.checkpoint is not None
                assert "drafts" not in interrupted.checkpoint
                assert "Evergreen one" not in repr(interrupted.checkpoint)
                assert await _count(session, ContentItem, channel_id=channel.id) == 3
                assert await _count(session, AdminAgentRunArtifact) == 3
                assert provider_calls == 1

                monkeypatch.setattr(
                    AdminAgentRunner,
                    "_result_from_artifacts",
                    original_result,
                )
                resumed = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).resume_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    run_id=interrupted.id,
                )
                assert resumed.status == "completed"
                assert provider_calls == 1
                assert await _count(session, ContentItem, channel_id=channel.id) == 3
                assert len(resumed.result["drafts"]) == 3

                completed_again = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                ).resume_drafts_tomorrow(
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    run_id=interrupted.id,
                )
                assert completed_again.id == resumed.id
                assert completed_again.result == resumed.result
                assert await _count(session, ContentItem, channel_id=channel.id) == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_resume_has_one_executor(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        database_path = tmp_path / "concurrent-resume.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        now = datetime.now(timezone.utc)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as setup_session:
                owner, channel = await _setup_channel(setup_session, tg_user_id=9935)
                channel_id = int(channel.id)
                owner_tg_user_id = int(owner.tg_user_id)
                run_row = AdminAgentRun(
                    owner_tg_user_id=owner_tg_user_id,
                    channel_id=channel_id,
                    scenario="drafts_tomorrow",
                    request_id="concurrent-resume-0001",
                    skill_id="drafts_tomorrow",
                    skill_version="1",
                    workflow_phase=PHASE_GENERATION_VALIDATED,
                    checkpoint={
                        "checkpoint_version": 1,
                        "state": PHASE_GENERATION_VALIDATED,
                        "target_local_date": "2026-09-20",
                        "timezone": "UTC",
                        "context_summary": {
                            "recent_count": 0,
                            "scheduled_count": 0,
                            "item_count": 0,
                            "total_excerpt_chars": 0,
                            "fingerprint": "0" * 64,
                            "refs": [],
                        },
                        "drafts": _drafts(),
                    },
                    status="failed",
                    started_at=now,
                )
                setup_session.add(run_row)
                await setup_session.commit()
                await setup_session.refresh(run_row)
                run_id = int(run_row.id)

            entered = asyncio.Event()
            release = asyncio.Event()
            original_persist = AdminAgentRunner._persist_validated_drafts

            async def hold_persist(self, run_row):
                entered.set()
                await release.wait()
                return await original_persist(self, run_row)

            monkeypatch.setattr(
                AdminAgentRunner,
                "_persist_validated_drafts",
                hold_persist,
            )

            async with Session() as first_session, Session() as second_session:
                first_task = asyncio.create_task(
                    AdminAgentRunner(
                        first_session,
                        limits=DRAFT_SCENARIO_LIMITS,
                        now_utc=now,
                    ).resume_drafts_tomorrow(
                        channel_id=channel_id,
                        owner_tg_user_id=owner_tg_user_id,
                        run_id=run_id,
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=2.0)

                with pytest.raises(AgentExecutionBusy):
                    await AdminAgentRunner(
                        second_session,
                        limits=DRAFT_SCENARIO_LIMITS,
                        now_utc=now,
                    ).resume_drafts_tomorrow(
                        channel_id=channel_id,
                        owner_tg_user_id=owner_tg_user_id,
                        run_id=run_id,
                    )

                release.set()
                completed = await asyncio.wait_for(first_task, timeout=5.0)
                assert completed.status == "completed"
                assert completed.execution_claim_token is None

            async with Session() as verify_session:
                assert await _count(
                    verify_session,
                    ContentItem,
                    channel_id=channel_id,
                ) == 3
                assert await _count(verify_session, AdminAgentRunArtifact) == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_partial_persisted_artifact_set_fails_closed_without_new_drafts() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        now = datetime.now(timezone.utc)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9939)
                channel_id = int(channel.id)
                owner_tg_user_id = int(owner.tg_user_id)
                item = await ContentRepo(session).create(
                    channel_id=channel_id,
                    title="Existing owned draft",
                    document=PostDocument(
                        blocks=[{"id": "existing", "type": "text", "text": "Canonical draft"}]
                    ),
                    created_by_tg_user_id=owner_tg_user_id,
                    source="admin_agent",
                )
                item_id = int(item.id)
                run_row = AdminAgentRun(
                    owner_tg_user_id=owner_tg_user_id,
                    channel_id=channel_id,
                    scenario="drafts_tomorrow",
                    request_id="partial-artifacts-0001",
                    skill_id="drafts_tomorrow",
                    skill_version="1",
                    workflow_phase=PHASE_DRAFTS_PERSISTED,
                    checkpoint={
                        "checkpoint_version": 1,
                        "state": PHASE_DRAFTS_PERSISTED,
                        "target_local_date": "2026-09-20",
                        "timezone": "UTC",
                        "context_summary": {
                            "recent_count": 0,
                            "scheduled_count": 0,
                            "item_count": 0,
                            "total_excerpt_chars": 0,
                            "fingerprint": "0" * 64,
                            "refs": [],
                        },
                    },
                    status="failed",
                    started_at=now,
                )
                session.add(run_row)
                await session.flush()
                session.add(
                    AdminAgentRunArtifact(
                        run_id=run_row.id,
                        artifact_type="content_draft",
                        ordinal=1,
                        content_item_id=item_id,
                        content_revision=1,
                    )
                )
                await session.commit()
                run_id = int(run_row.id)
                before = await _count(session, ContentItem, channel_id=channel_id)

                resumed = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                    now_utc=now,
                ).resume_drafts_tomorrow(
                    channel_id=channel_id,
                    owner_tg_user_id=owner_tg_user_id,
                    run_id=run_id,
                )
                assert resumed.status == "failed"
                assert resumed.workflow_phase == PHASE_FAILED_CLOSED
                assert resumed.checkpoint is None
                assert await _count(session, ContentItem, channel_id=channel_id) == before
                assert await _count(session, AdminAgentRunArtifact) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_resume_claim_and_fail_closed_states() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        now = datetime.now(timezone.utc)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9941)
                channel_id = int(channel.id)
                owner_tg_user_id = int(owner.tg_user_id)
                run_row = AdminAgentRun(
                    owner_tg_user_id=owner_tg_user_id,
                    channel_id=channel_id,
                    scenario="drafts_tomorrow",
                    request_id="claim-test-0001",
                    skill_id="drafts_tomorrow",
                    skill_version="1",
                    workflow_phase=PHASE_GENERATION_VALIDATED,
                    checkpoint={
                        "checkpoint_version": 1,
                        "state": PHASE_GENERATION_VALIDATED,
                        "target_local_date": "2026-09-20",
                        "timezone": "UTC",
                        "context_summary": {
                            "recent_count": 0,
                            "scheduled_count": 0,
                            "item_count": 0,
                            "total_excerpt_chars": 0,
                            "fingerprint": "0" * 64,
                            "refs": [],
                        },
                        "drafts": _drafts(),
                    },
                    execution_claim_token="fresh-claim",
                    execution_claimed_at=now,
                    status="failed",
                    started_at=now,
                )
                session.add(run_row)
                await session.commit()
                await session.refresh(run_row)
                run_id = int(run_row.id)

                with pytest.raises(AgentExecutionBusy):
                    await AdminAgentRunner(
                        session,
                        limits=DRAFT_SCENARIO_LIMITS,
                        now_utc=now,
                    ).resume_drafts_tomorrow(
                        channel_id=channel_id,
                        owner_tg_user_id=owner_tg_user_id,
                        run_id=run_id,
                    )

                run_row = await session.get(AdminAgentRun, run_id)
                assert run_row is not None
                run_row.execution_claimed_at = now - timedelta(minutes=5)
                await session.commit()
                recovered = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                    now_utc=now,
                ).resume_drafts_tomorrow(
                    channel_id=channel_id,
                    owner_tg_user_id=owner_tg_user_id,
                    run_id=run_id,
                )
                assert recovered.status == "completed"
                assert recovered.execution_claim_token is None
                assert await _count(session, ContentItem, channel_id=channel_id) == 3

                unknown = AdminAgentRun(
                    owner_tg_user_id=owner_tg_user_id,
                    channel_id=channel_id,
                    scenario="drafts_tomorrow",
                    request_id="unknown-version-0001",
                    skill_id="drafts_tomorrow",
                    skill_version="999",
                    workflow_phase=PHASE_GENERATION_VALIDATED,
                    checkpoint={},
                    status="failed",
                    started_at=now,
                )
                session.add(unknown)
                await session.commit()
                await session.refresh(unknown)
                with pytest.raises(AgentResumeError, match="unsupported"):
                    await AdminAgentRunner(
                        session,
                        limits=DRAFT_SCENARIO_LIMITS,
                        now_utc=now,
                    ).resume_drafts_tomorrow(
                        channel_id=channel_id,
                        owner_tg_user_id=owner_tg_user_id,
                        run_id=unknown.id,
                    )

                malformed = AdminAgentRun(
                    owner_tg_user_id=owner_tg_user_id,
                    channel_id=channel_id,
                    scenario="drafts_tomorrow",
                    request_id="malformed-0001",
                    skill_id="drafts_tomorrow",
                    skill_version="1",
                    workflow_phase=PHASE_GENERATION_VALIDATED,
                    checkpoint={"checkpoint_version": 1, "state": PHASE_GENERATION_VALIDATED},
                    status="failed",
                    started_at=now,
                )
                session.add(malformed)
                await session.commit()
                await session.refresh(malformed)
                failed_closed = await AdminAgentRunner(
                    session,
                    limits=DRAFT_SCENARIO_LIMITS,
                    now_utc=now,
                ).resume_drafts_tomorrow(
                    channel_id=channel_id,
                    owner_tg_user_id=owner_tg_user_id,
                    run_id=malformed.id,
                )
                assert failed_closed.status == "failed"
                assert failed_closed.workflow_phase == PHASE_FAILED_CLOSED
                assert failed_closed.checkpoint is None
                assert failed_closed.execution_claim_token is None
        finally:
            await engine.dispose()

    asyncio.run(run())
