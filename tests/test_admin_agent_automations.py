from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import (
    AdminAgentApproval,
    AdminAgentAutomation,
    AdminAgentRun,
)
from app.domain.content.models import ContentItem
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.admin_agent import AdminAgentRunner
from app.services.admin_agent_automations import (
    AutomationIdempotencyConflict,
    AutomationInputError,
    AdminAgentAutomationService,
    AdminAgentAutomationTickService,
    CLAIM_LEASE,
    MAX_OCCURRENCES_PER_TICK,
    MISFIRE_GRACE,
    next_occurrence_utc,
    normalize_automation_operator_input,
    occurrence_request_id,
)
from app.services.admin_agent_skills import (
    AUTOMATION_BOUNDED,
    AdminAgentSkillSpec,
    AdminAgentSkillRegistry,
    SKILL_REGISTRY,
)
from app.services.ai_generation import AIGenerationService


async def _setup_channel(session, tg_user_id: int = 16001):
    owner = await ClientsRepo(session).create_or_get(
        tg_user_id,
        f"automation_{tg_user_id}",
        "Automation Owner",
    )
    channel = await ChannelsRepo(session).create(
        owner.id,
        -3000000 - tg_user_id,
        "Automation channel",
    )
    ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
    ai.enabled = True
    ai.model = "provider/model"
    await session.commit()
    return owner, channel


async def _create_due(
    session,
    *,
    owner,
    channel,
    request_id: str,
    skill_id: str = "attention_today",
    skill_version: str = "1",
    operator_input: dict | None = None,
    now: datetime,
) -> AdminAgentAutomation:
    row = await AdminAgentAutomationService(session).create(
        owner_tg_user_id=owner.tg_user_id,
        channel_id=channel.id,
        request_id=request_id,
        skill_id=skill_id,
        skill_version=skill_version,
        operator_input=operator_input or {},
        cadence_kind="daily",
        local_time_value="09:00",
        weekday=None,
        now_utc=now,
    )
    row.next_run_at = now
    row.claim_token = None
    row.claimed_at = None
    await session.commit()
    await session.refresh(row)
    return row


def _draft_payload() -> str:
    return json.dumps(
        {
            "drafts": [
                {"title": "Практический разбор", "text": "Evergreen текст один без текущих новостей."},
                {"title": "Вопрос аудитории", "text": "Evergreen текст два без текущих новостей."},
                {"title": "Короткий чек-лист", "text": "Evergreen текст три без текущих новостей."},
            ]
        },
        ensure_ascii=False,
    )


def _series_payload(count: int = 3) -> str:
    return json.dumps(
        {
            "series": {
                "title": "Серия о рабочих процессах",
                "summary": "Самостоятельные evergreen материалы с разными углами.",
            },
            "posts": [
                {
                    "title": f"Практический пост {index}",
                    "angle": f"Самостоятельный угол {index}",
                    "objective": f"Дать применимый вывод {index}",
                    "text": f"Evergreen текст {index}: конкретный разбор без текущих новостей.",
                }
                for index in range(1, count + 1)
            ],
        },
        ensure_ascii=False,
    )


def test_registry_automation_policy_is_explicit_and_fail_closed() -> None:
    assert {
        (spec.skill_id, str(spec.version))
        for spec in SKILL_REGISTRY.automation_specs
    } == {
        ("attention_today", "1"),
        ("drafts_tomorrow", "1"),
        ("prepare_content_series", "1"),
    }
    assert all(
        spec.automation_policy == AUTOMATION_BOUNDED
        for spec in SKILL_REGISTRY.automation_specs
    )

    base = SKILL_REGISTRY.resolve("attention_today", "1")
    unsafe = AdminAgentSkillSpec(
        skill_id="future_unsafe",
        version="1",
        scenario=base.scenario,
        execution_limits=dict(base.execution_limits),
        allowed_capability_classes=base.allowed_capability_classes,
        context_profile=base.context_profile,
        resume_policy=base.resume_policy,
        display_title="Future",
        description="Future skill",
        category=base.category,
        operator_input_schema=dict(base.operator_input_schema),
        result_kind=base.result_kind,
        approval_requirement="explicit",
        capability_summary=base.capability_summary,
        context_requirements=base.context_requirements,
        automation_policy=AUTOMATION_BOUNDED,
    )
    with pytest.raises(ValueError, match="may not require approval"):
        AdminAgentSkillRegistry((unsafe,))


def test_closed_operator_input_and_local_wall_clock_cadence() -> None:
    attention = SKILL_REGISTRY.resolve("attention_today", "1")
    assert normalize_automation_operator_input(attention, {}) == {}
    with pytest.raises(AutomationInputError, match="must be empty"):
        normalize_automation_operator_input(attention, {"prompt": "arbitrary"})

    series = SKILL_REGISTRY.resolve("prepare_content_series", "1")
    normalized = normalize_automation_operator_input(
        series,
        {
            "brief": "   Сделай bounded evergreen серию о рабочих процессах.   ",
            "post_count": 3,
        },
    )
    assert normalized == {
        "brief": "Сделай bounded evergreen серию о рабочих процессах.",
        "post_count": 3,
    }
    with pytest.raises(AutomationInputError, match="only brief and post_count"):
        normalize_automation_operator_input(
            series,
            {
                "brief": "Сделай bounded evergreen серию о рабочих процессах.",
                "post_count": 3,
                "tool": "publish",
            },
        )

    before_dst = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
    first = next_occurrence_utc(
        cadence_kind="daily",
        local_time_value="09:30",
        weekday=None,
        timezone_name="Europe/Berlin",
        after_utc=before_dst,
    )
    second = next_occurrence_utc(
        cadence_kind="daily",
        local_time_value="09:30",
        weekday=None,
        timezone_name="Europe/Berlin",
        after_utc=first,
    )
    assert first.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin")).strftime("%H:%M") == "09:30"
    assert second.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin")).strftime("%H:%M") == "09:30"
    assert second - first == timedelta(hours=23)

    weekly = next_occurrence_utc(
        cadence_kind="weekly",
        local_time_value="10:15",
        weekday=0,
        timezone_name="UTC",
        after_utc=datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc),
    )
    assert weekly == datetime(2026, 9, 21, 10, 15, tzinfo=timezone.utc)


def test_definition_idempotency_pins_timezone_and_conflicts(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 16002)
                service = AdminAgentAutomationService(session)
                first = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    request_id="automation-definition-0001",
                    skill_id="attention_today",
                    skill_version="1",
                    operator_input={},
                    cadence_kind="daily",
                    local_time_value="10:00",
                    weekday=None,
                    now_utc=now,
                )
                assert first.timezone == "UTC+3"
                first_id = first.id

                # A later channel timezone must not rewrite an idempotent definition.
                session.add(
                    ScheduleEntry(
                        content_item_id=999001,
                        content_revision=1,
                        channel_id=channel.id,
                        scheduled_at=now + timedelta(hours=1),
                        timezone="Europe/Berlin",
                        status="pending",
                        repeat_rule={},
                        meta={},
                    )
                )
                await session.commit()
                same = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    request_id="automation-definition-0001",
                    skill_id="attention_today",
                    skill_version="1",
                    operator_input={},
                    cadence_kind="daily",
                    local_time_value="10:00",
                    weekday=None,
                    now_utc=now,
                )
                assert same.id == first_id
                assert same.timezone == "UTC+3"

                with pytest.raises(AutomationIdempotencyConflict):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        request_id="automation-definition-0001",
                        skill_id="attention_today",
                        skill_version="1",
                        operator_input={},
                        cadence_kind="daily",
                        local_time_value="11:00",
                        weekday=None,
                        now_utc=now,
                    )
                with pytest.raises(AutomationInputError, match="unknown skill version"):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        request_id="automation-definition-0002",
                        skill_id="attention_today",
                        skill_version="999",
                        operator_input={},
                        cadence_kind="daily",
                        local_time_value="10:00",
                        weekday=None,
                        now_utc=now,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_due_claim_single_winner_expiry_recovery_and_tick_bound() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 16003)
                await _create_due(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="automation-claim-0001",
                    now=now,
                )

            worker_a = AdminAgentAutomationTickService(Session)
            worker_b = AdminAgentAutomationTickService(Session)
            first = await worker_a._claim_one(now)
            assert first is not None
            assert await worker_b._claim_one(now) is None

            automation_id, first_token = first
            async with Session() as session:
                row = await session.get(AdminAgentAutomation, automation_id)
                assert row is not None
                assert row.claim_token == first_token
                row.claimed_at = now - CLAIM_LEASE - timedelta(seconds=1)
                await session.commit()

            recovered = await worker_b._claim_one(now)
            assert recovered is not None
            assert recovered[0] == automation_id
            assert recovered[1] != first_token

            async with Session() as session:
                row = await session.get(AdminAgentAutomation, automation_id)
                assert row is not None
                row.enabled = False
                row.claim_token = None
                row.claimed_at = None
                await session.commit()
                for index in range(MAX_OCCURRENCES_PER_TICK + 2):
                    await _create_due(
                        session,
                        owner=owner,
                        channel=channel,
                        request_id=f"automation-bound-{index:04d}",
                        now=now,
                    )

            calls = 0
            original = AdminAgentRunner.run_exact_skill

            async def fake_run_exact(self, **kwargs):
                nonlocal calls
                calls += 1
                return None

            AdminAgentRunner.run_exact_skill = fake_run_exact
            try:
                processed = await AdminAgentAutomationTickService(Session).tick(
                    now_utc=now
                )
            finally:
                AdminAgentRunner.run_exact_skill = original
            assert processed == MAX_OCCURRENCES_PER_TICK
            assert calls == MAX_OCCURRENCES_PER_TICK
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_misfire_existing_run_and_ownership_fail_closed_without_replay(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)

            async def forbidden(self, **kwargs):
                raise AssertionError("existing/misfired occurrence must not execute")

            monkeypatch.setattr(AdminAgentRunner, "run_exact_skill", forbidden)

            async with Session() as session:
                owner, channel = await _setup_channel(session, 16004)
                misfire = await _create_due(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="automation-misfire-0001",
                    now=now - MISFIRE_GRACE - timedelta(minutes=1),
                )
                misfire.next_run_at = now - MISFIRE_GRACE - timedelta(minutes=1)

                existing = await _create_due(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="automation-existing-0001",
                    now=now,
                )
                session.add(
                    AdminAgentRun(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        scenario="attention_today",
                        request_id=occurrence_request_id(existing.id, now),
                        operator_input={},
                        skill_id="attention_today",
                        skill_version="1",
                        automation_id=existing.id,
                        scheduled_for=now,
                        workflow_phase="generation_validated",
                        status="running",
                        tokens_used=0,
                    )
                )
                ownership = await _create_due(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="automation-ownership-0001",
                    now=now,
                )
                other = await ClientsRepo(session).create_or_get(
                    16005, "other_owner", "Other"
                )
                await session.commit()

            # Process misfire first, then existing occurrence, then ownership change.
            worker = AdminAgentAutomationTickService(Session, max_occurrences=1)
            assert await worker.tick(now_utc=now) == 1
            assert await worker.tick(now_utc=now) == 1

            async with Session() as session:
                channel_row = await session.get(type(channel), channel.id)
                assert channel_row is not None
                channel_row.owner_id = other.id
                await session.commit()

            assert await worker.tick(now_utc=now) == 1
            async with Session() as session:
                misfire_row = await session.get(AdminAgentAutomation, misfire.id)
                existing_row = await session.get(AdminAgentAutomation, existing.id)
                ownership_row = await session.get(AdminAgentAutomation, ownership.id)
                assert misfire_row is not None and misfire_row.last_scheduled_for is not None
                assert existing_row is not None and existing_row.last_scheduled_for is not None
                assert ownership_row is not None and ownership_row.enabled is False
                assert (
                    await session.execute(
                        select(func.count(AdminAgentRun.id)).where(
                            AdminAgentRun.automation_id == existing.id
                        )
                    )
                ).scalar_one() == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("skill_id", "operator_input", "payload", "expected_drafts"),
    [
        ("drafts_tomorrow", {}, _draft_payload(), 3),
        (
            "prepare_content_series",
            {
                "brief": "Сделай bounded evergreen серию о рабочих процессах.",
                "post_count": 3,
            },
            _series_payload(3),
            3,
        ),
    ],
)
def test_crash_after_run_before_advance_never_replays_llm_or_drafts(
    monkeypatch,
    skill_id,
    operator_input,
    payload,
    expected_drafts,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
            calls = 0

            async def generated(self, **kwargs):
                nonlocal calls
                calls += 1
                return {
                    "success": True,
                    "text": payload,
                    "tokens_used": 17,
                    "model": "provider/model",
                    "error": None,
                }

            monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)

            async with Session() as session:
                owner, channel = await _setup_channel(
                    session,
                    16100 if skill_id == "drafts_tomorrow" else 16101,
                )
                automation = await _create_due(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id=f"automation-crash-{skill_id}-0001",
                    skill_id=skill_id,
                    operator_input=operator_input,
                    now=now,
                )

            worker = AdminAgentAutomationTickService(Session, max_occurrences=1)
            original_advance = worker._advance_claimed
            crash_once = True

            async def crash_before_advance(
                session,
                row,
                *,
                claim_token,
                scheduled_for,
                after_utc,
            ):
                nonlocal crash_once
                if crash_once:
                    crash_once = False
                    raise RuntimeError("simulated crash after durable run")
                return await original_advance(
                    session,
                    row,
                    claim_token=claim_token,
                    scheduled_for=scheduled_for,
                    after_utc=after_utc,
                )

            monkeypatch.setattr(worker, "_advance_claimed", crash_before_advance)
            assert await worker.tick(now_utc=now) == 1
            assert calls == 1

            async with Session() as session:
                row = await session.get(AdminAgentAutomation, automation.id)
                assert row is not None
                row.claimed_at = now - CLAIM_LEASE - timedelta(seconds=1)
                await session.commit()
                assert (
                    await session.execute(
                        select(func.count(AdminAgentRun.id)).where(
                            AdminAgentRun.automation_id == automation.id
                        )
                    )
                ).scalar_one() == 1
                assert (
                    await session.execute(
                        select(func.count(ContentItem.id)).where(
                            ContentItem.channel_id == channel.id
                        )
                    )
                ).scalar_one() == expected_drafts

            monkeypatch.setattr(worker, "_advance_claimed", original_advance)
            assert await worker.tick(now_utc=now) == 1
            assert calls == 1

            async with Session() as session:
                assert (
                    await session.execute(
                        select(func.count(AdminAgentRun.id)).where(
                            AdminAgentRun.automation_id == automation.id
                        )
                    )
                ).scalar_one() == 1
                assert (
                    await session.execute(
                        select(func.count(ContentItem.id)).where(
                            ContentItem.channel_id == channel.id
                        )
                    )
                ).scalar_one() == expected_drafts
                assert (
                    await session.execute(
                        select(func.count(ScheduleEntry.id)).where(
                            ScheduleEntry.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 0
                assert (
                    await session.execute(
                        select(func.count(Publication.id)).where(
                            Publication.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 0
                assert (
                    await session.execute(
                        select(func.count(AdminAgentApproval.id)).where(
                            AdminAgentApproval.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unsupported_pinned_version_disables_definition_without_execution(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 16006)
                row = await _create_due(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="automation-version-0001",
                    now=now,
                )
                row.skill_version = "999"
                await session.commit()

            async def forbidden(self, **kwargs):
                raise AssertionError("unsupported exact version must not execute")

            monkeypatch.setattr(AdminAgentRunner, "run_exact_skill", forbidden)
            assert await AdminAgentAutomationTickService(Session).tick(now_utc=now) == 1
            async with Session() as session:
                row = await session.get(AdminAgentAutomation, row.id)
                assert row is not None
                assert row.enabled is False
                assert (
                    await session.execute(select(func.count(AdminAgentRun.id)))
                ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_admin_automation_path_has_no_forbidden_side_effect_stack() -> None:
    source = inspect.getsource(AdminAgentAutomationTickService)
    for forbidden in (
        "AdminAgentApprovalService",
        "AdminAgentSeriesApprovalService",
        "LegacyPublicationBridge",
        "send_message",
        "_send_to_owner",
        "tg_bot",
        "Publication",
        "ScheduleEntry",
    ):
        assert forbidden not in source
