from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import AdminAgentAutomation, AdminAgentRun
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.admin_agent import AdminAgentRunner
from app.services.admin_agent_automations import (
    DISABLED_INVALID_DEFINITION,
    DISABLED_MANUAL_PAUSE,
    DISABLED_OWNERSHIP_LOST,
    DISABLED_UNSUPPORTED_SKILL_VERSION,
    HEALTH_ACTIVE,
    HEALTH_BLOCKED,
    HEALTH_NEEDS_ATTENTION,
    HEALTH_PAUSED,
    OUTCOME_MISFIRE_SKIPPED,
    OUTCOME_RUN_RECORDED,
    OUTCOME_SAFETY_DISABLED,
    AutomationControlConflict,
    AdminAgentAutomationService,
    AdminAgentAutomationTickService,
    MISFIRE_GRACE,
    automation_definition_fingerprint,
)
from app.services.admin_agent_skills import SKILL_REGISTRY


async def _setup_channel(session, tg_user_id: int):
    owner = await ClientsRepo(session).create_or_get(
        tg_user_id,
        f"e5_{tg_user_id}",
        "E5 Owner",
    )
    channel = await ChannelsRepo(session).create(
        owner.id,
        -4000000 - tg_user_id,
        "E5 channel",
    )
    ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
    ai.enabled = True
    ai.model = "provider/model"
    await session.commit()
    return owner, channel


async def _create(
    session,
    *,
    owner,
    channel,
    request_id: str,
    now: datetime,
    skill_id: str = "attention_today",
    operator_input: dict | None = None,
) -> AdminAgentAutomation:
    return await AdminAgentAutomationService(session).create(
        owner_tg_user_id=owner.tg_user_id,
        channel_id=channel.id,
        request_id=request_id,
        skill_id=skill_id,
        skill_version="1",
        operator_input=operator_input or {},
        cadence_kind="daily",
        local_time_value="09:00",
        weekday=None,
        now_utc=now,
    )


def _refingerprint(row: AdminAgentAutomation) -> None:
    row.definition_fingerprint = automation_definition_fingerprint(
        skill_id=str(row.skill_id),
        skill_version=str(row.skill_version),
        operator_input=dict(row.operator_input or {}),
        cadence_kind=str(row.cadence_kind),
        local_time_value=str(row.local_time),
        weekday=row.weekday,
        timezone_name=str(row.timezone),
    )


def test_manual_pause_and_safe_reenable_recompute_future_occurrence() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 17001)
                row = await _create(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="e5-manual-pause-0001",
                    now=now,
                )
                service = AdminAgentAutomationService(session)
                original_next_run_at = row.next_run_at
                row.claim_token = "live-claim-token"
                row.claimed_at = now
                await session.commit()
                unchanged = await service.set_enabled(
                    automation_id=row.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    enabled=True,
                    now_utc=now + timedelta(minutes=1),
                )
                assert unchanged is not None
                assert unchanged.enabled is True
                assert unchanged.next_run_at == original_next_run_at
                assert unchanged.claim_token == "live-claim-token"
                assert unchanged.claimed_at == now

                paused = await service.set_enabled(
                    automation_id=row.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    enabled=False,
                    now_utc=now,
                )
                assert paused is not None
                assert paused.enabled is False
                assert paused.disabled_reason == DISABLED_MANUAL_PAUSE
                assert paused.disabled_at is not None
                assert paused.disabled_at.replace(tzinfo=timezone.utc) == now
                assert paused.claim_token is None

                enabled = await service.set_enabled(
                    automation_id=row.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    enabled=True,
                    now_utc=now + timedelta(days=2, hours=1),
                )
                assert enabled is not None
                assert enabled.enabled is True
                assert enabled.disabled_reason is None
                assert enabled.disabled_at is None
                assert enabled.next_run_at > now + timedelta(days=2, hours=1)
                assert enabled.last_scheduled_for is None
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("mode", "expected_reason"),
    [
        ("unsupported", DISABLED_UNSUPPORTED_SKILL_VERSION),
        ("invalid", DISABLED_INVALID_DEFINITION),
        ("ownership", DISABLED_OWNERSHIP_LOST),
    ],
)
def test_reenable_fail_closed_for_unsafe_definition_or_ownership(
    mode: str,
    expected_reason: str,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 17010)
                row = await _create(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id=f"e5-reenable-{mode}-0001",
                    now=now,
                )
                service = AdminAgentAutomationService(session)
                await service.set_enabled(
                    automation_id=row.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    enabled=False,
                    now_utc=now,
                )
                if mode == "unsupported":
                    row.skill_version = "999"
                    _refingerprint(row)
                elif mode == "invalid":
                    row.definition_fingerprint = "0" * 64
                else:
                    other = await ClientsRepo(session).create_or_get(
                        17011, "e5_other", "Other"
                    )
                    channel.owner_id = other.id
                await session.commit()

                with pytest.raises(AutomationControlConflict) as exc:
                    await service.set_enabled(
                        automation_id=row.id,
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        enabled=True,
                        now_utc=now + timedelta(minutes=1),
                    )
                assert exc.value.reason == expected_reason
                await session.refresh(row)
                assert row.enabled is False
                assert row.disabled_reason == expected_reason
                assert row.last_outcome == OUTCOME_SAFETY_DISABLED
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("mode", "expected_reason"),
    [
        ("unsupported", DISABLED_UNSUPPORTED_SKILL_VERSION),
        ("invalid", DISABLED_INVALID_DEFINITION),
        ("ownership", DISABLED_OWNERSHIP_LOST),
    ],
)
def test_worker_safety_disable_reason_is_durable_and_never_executes(
    monkeypatch,
    mode: str,
    expected_reason: str,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 17020)
                row = await _create(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id=f"e5-worker-{mode}-0001",
                    now=now,
                )
                row.next_run_at = now
                if mode == "unsupported":
                    row.skill_version = "999"
                    _refingerprint(row)
                elif mode == "invalid":
                    row.definition_fingerprint = "f" * 64
                else:
                    other = await ClientsRepo(session).create_or_get(
                        17021, "e5_worker_other", "Other"
                    )
                    channel.owner_id = other.id
                await session.commit()
                row_id = row.id

            async def forbidden(self, **kwargs):
                raise AssertionError("unsafe automation must never execute")

            monkeypatch.setattr(AdminAgentRunner, "run_exact_skill", forbidden)
            assert await AdminAgentAutomationTickService(Session).tick(now_utc=now) == 1

            async with Session() as session:
                stored = await session.get(AdminAgentAutomation, row_id)
                assert stored is not None
                assert stored.enabled is False
                assert stored.disabled_reason == expected_reason
                assert stored.last_outcome == OUTCOME_SAFETY_DISABLED
                assert (
                    await session.execute(select(func.count(AdminAgentRun.id)))
                ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_misfire_sets_outcome_and_zero_run() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 17030)
                row = await _create(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="e5-misfire-0001",
                    now=now,
                )
                scheduled = now - MISFIRE_GRACE - timedelta(seconds=1)
                row.next_run_at = scheduled
                await session.commit()
                row_id = row.id

            assert await AdminAgentAutomationTickService(Session).tick(now_utc=now) == 1
            async with Session() as session:
                stored = await session.get(AdminAgentAutomation, row_id)
                assert stored is not None
                assert stored.last_scheduled_for is not None
                assert stored.last_scheduled_for.replace(tzinfo=timezone.utc) == scheduled
                assert stored.last_outcome == OUTCOME_MISFIRE_SKIPPED
                assert stored.enabled is True
                assert (
                    await session.execute(select(func.count(AdminAgentRun.id)))
                ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_health_usage_and_migration_suggestion_are_derived_not_persisted() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 17040)
                row = await _create(
                    session,
                    owner=owner,
                    channel=channel,
                    request_id="e5-health-0001",
                    now=now,
                    skill_id="drafts_tomorrow",
                )
                service = AdminAgentAutomationService(session)

                active = await service.operational_snapshot(row, now_utc=now)
                assert active["health"] == HEALTH_ACTIVE
                assert active["execution_limits"]["max_llm_calls"] == 1
                assert active["cadence_occurrences_per_week"] == 7

                session.add_all(
                    [
                        AdminAgentRun(
                            owner_tg_user_id=owner.tg_user_id,
                            channel_id=channel.id,
                            scenario="drafts_tomorrow",
                            request_id="e5-health-run-1",
                            operator_input={},
                            skill_id="drafts_tomorrow",
                            skill_version="1",
                            automation_id=row.id,
                            scheduled_for=now - timedelta(days=1),
                            workflow_phase="completed",
                            status="completed",
                            tokens_used=11,
                        ),
                        AdminAgentRun(
                            owner_tg_user_id=owner.tg_user_id,
                            channel_id=channel.id,
                            scenario="drafts_tomorrow",
                            request_id="e5-health-run-2",
                            operator_input={},
                            skill_id="drafts_tomorrow",
                            skill_version="1",
                            automation_id=row.id,
                            scheduled_for=now - timedelta(days=8),
                            workflow_phase="failed",
                            status="failed",
                            tokens_used=13,
                        ),
                        AdminAgentRun(
                            owner_tg_user_id=owner.tg_user_id,
                            channel_id=channel.id,
                            scenario="drafts_tomorrow",
                            request_id="e5-health-run-3",
                            operator_input={},
                            skill_id="drafts_tomorrow",
                            skill_version="1",
                            automation_id=row.id,
                            scheduled_for=now - timedelta(minutes=1),
                            workflow_phase="restart_required",
                            status="failed",
                            tokens_used=17,
                        ),
                    ]
                )
                await session.commit()

                attention = await service.operational_snapshot(row, now_utc=now)
                assert attention["health"] == HEALTH_NEEDS_ATTENTION
                assert attention["health_reason"] == "restart_required"
                assert attention["usage_7d"] == {
                    "occurrence_runs": 2,
                    "completed": 1,
                    "failed": 1,
                    "restart_required_or_manual_resume": 1,
                    "tokens_used": 28,
                }
                assert attention["usage_30d"] == {
                    "occurrence_runs": 3,
                    "completed": 1,
                    "failed": 2,
                    "restart_required_or_manual_resume": 1,
                    "tokens_used": 41,
                }

                row.enabled = False
                row.disabled_reason = DISABLED_MANUAL_PAUSE
                await session.commit()
                paused = await service.operational_snapshot(row, now_utc=now)
                assert paused["health"] == HEALTH_PAUSED

                # A current safety blocker must outrank a historical manual pause.
                row.skill_version = "999"
                _refingerprint(row)
                await session.commit()
                blocked = await service.operational_snapshot(row, now_utc=now)
                assert blocked["health"] == HEALTH_BLOCKED
                assert blocked["health_reason"] == DISABLED_UNSUPPORTED_SKILL_VERSION
                assert blocked["migration_suggestion"] == {
                    "migration_available": True,
                    "suggested_skill_id": "drafts_tomorrow",
                    "suggested_skill_version": "1",
                    "operator_input": {},
                }
                assert row.skill_version == "999"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_worker_keeps_e4_no_retry_no_resume_execution_shape() -> None:
    source = inspect.getsource(AdminAgentAutomationTickService)
    assert "resume_" not in source
    assert "retry" not in source.lower()
    assert "AdminAgentRunner" in source
    assert "run_exact_skill" in source


def test_current_version_lookup_is_read_only() -> None:
    current = SKILL_REGISTRY.current_for_skill_id("prepare_content_series")
    assert current.skill_id == "prepare_content_series"
    assert current.version == "1"
