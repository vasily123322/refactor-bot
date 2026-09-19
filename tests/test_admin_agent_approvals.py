from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import AdminAgentApproval, AdminAgentRun
from app.domain.content import PostDocument
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo
from app.services.admin_agent_approvals import (
    ACTION_SCHEDULE_DRAFT_TOMORROW,
    STATE_EXECUTED,
    STATE_EXECUTING,
    STATE_PENDING_REVIEW,
    STATE_REJECTED,
    STATE_STALE,
    ApprovalExecutionError,
    ApprovalInputError,
    AdminAgentApprovalService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import as_utc


async def _setup_channel(session, *, tg_user_id: int = 9911):
    owner = await ClientsRepo(session).create_or_get(
        tg_user_id,
        f"owner_{tg_user_id}",
        "Owner",
    )
    channel = await ChannelsRepo(session).create(
        owner.id,
        -2000000 - tg_user_id,
        "Approval channel",
    )
    return owner, channel


async def _draft(
    session,
    *,
    channel_id: int,
    owner_tg_user_id: int,
    title: str = "Обычный черновик",
    metadata: dict | None = None,
):
    return await ContentRepo(session).create(
        channel_id=channel_id,
        document=PostDocument(
            blocks=[{"id": "b1", "type": "text", "text": "Проверяемый текст черновика"}]
        ),
        status="draft",
        title=title,
        created_by_tg_user_id=owner_tg_user_id,
        source="admin_agent",
        metadata=metadata,
    )


async def _counts(session, channel_id: int) -> tuple[int, int]:
    schedule_count = int(
        (
            await session.execute(
                select(func.count(ScheduleEntry.id)).where(
                    ScheduleEntry.channel_id == int(channel_id)
                )
            )
        ).scalar_one()
    )
    publication_count = int(
        (
            await session.execute(
                select(func.count(Publication.id)).where(
                    Publication.channel_id == int(channel_id)
                )
            )
        ).scalar_one()
    )
    return schedule_count, publication_count


def test_proposal_captures_server_authority_tomorrow_and_is_idempotent() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session)
                source_run = AdminAgentRun(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    scenario="drafts_tomorrow",
                    request_id="source-draft-0001",
                    status="completed",
                    model=None,
                    tokens_used=0,
                    result={},
                    error=None,
                )
                session.add(source_run)
                await session.commit()
                await session.refresh(source_run)

                anchor = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="Timezone anchor",
                )
                session.add(
                    ScheduleEntry(
                        content_item_id=anchor.id,
                        content_revision=anchor.current_revision,
                        channel_id=channel.id,
                        scheduled_at=datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc),
                        timezone="America/Los_Angeles",
                        status="completed",
                        repeat_rule={},
                        meta={},
                    )
                )
                await session.commit()

                item = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    metadata={
                        "admin_agent_run_id": source_run.id,
                        "admin_agent_scenario": "drafts_tomorrow",
                    },
                )
                now = datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc)
                service = AdminAgentApprovalService(session, now_utc=now)
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="14:30",
                    request_id="approval-request-0001",
                )

                assert approval.state == STATE_PENDING_REVIEW
                assert approval.action_type == ACTION_SCHEDULE_DRAFT_TOMORROW
                assert approval.owner_tg_user_id == owner.tg_user_id
                assert approval.channel_id == channel.id
                assert approval.source_admin_agent_run_id == source_run.id
                assert approval.content_item_id == item.id
                assert approval.content_revision == item.current_revision == 1
                assert approval.timezone == "America/Los_Angeles"
                assert approval.target_local_date.isoformat() == "2026-09-20"
                assert approval.local_time == "14:30"
                assert as_utc(approval.resolved_scheduled_at) == datetime(
                    2026, 9, 20, 21, 30, tzinfo=timezone.utc
                )
                assert len(approval.action_fingerprint) == 64
                assert approval.execution_key is None
                assert await _counts(session, channel.id) == (1, 0)

                retry = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="18:45",
                    request_id="approval-request-0001",
                )
                assert retry.id == approval.id
                assert retry.local_time == "14:30"
                assert await _counts(session, channel.id) == (1, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_proposal_fallback_timezone_and_malformed_time_fail_closed() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9912)
                item = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                )
                service = AdminAgentApprovalService(
                    session,
                    now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                )
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="14:30",
                    request_id="approval-fallback-0001",
                )
                assert approval.timezone == "UTC+3"
                assert approval.target_local_date.isoformat() == "2026-09-20"
                assert as_utc(approval.resolved_scheduled_at) == datetime(
                    2026, 9, 20, 11, 30, tzinfo=timezone.utc
                )

                with pytest.raises(ApprovalInputError, match="HH:MM"):
                    await service.create_schedule_draft_tomorrow(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        content_item_id=item.id,
                        local_time_value="25:99",
                        request_id="approval-fallback-0002",
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reject_is_durable_idempotent_and_never_schedules() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9913)
                item = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                )
                service = AdminAgentApprovalService(
                    session,
                    now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                )
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="16:00",
                    request_id="approval-reject-0001",
                )
                rejected = await service.reject(
                    approval_id=approval.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert rejected is not None
                assert rejected.state == STATE_REJECTED
                assert rejected.reviewed_at is not None
                assert await _counts(session, channel.id) == (0, 0)

                retry = await service.approve(
                    approval_id=approval.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert retry is not None
                assert retry.state == STATE_REJECTED
                assert await _counts(session, channel.id) == (0, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_approve_uses_canonical_facade_once_and_has_no_provider_side_effect(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9914)
                item = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                )
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                service = AdminAgentApprovalService(session, now_utc=now)
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="17:15",
                    request_id="approval-execute-0001",
                )

                original_queue = LegacyPublicationBridge.queue
                calls: list[dict] = []

                async def tracked_queue(self, **kwargs):
                    calls.append(dict(kwargs))
                    return await original_queue(self, **kwargs)

                monkeypatch.setattr(LegacyPublicationBridge, "queue", tracked_queue)
                executed = await service.approve(
                    approval_id=approval.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert executed is not None
                assert executed.state == STATE_EXECUTED
                assert len(calls) == 1
                assert calls[0]["content_item_id"] == item.id
                assert calls[0]["content_revision"] == 1
                assert calls[0]["repeat_rule"] is None
                assert calls[0]["runtime_options"] is None
                assert calls[0]["metadata"]["admin_agent_approval_id"] == approval.id
                assert calls[0]["metadata"]["admin_agent_execution_key"] == executed.execution_key

                assert await _counts(session, channel.id) == (1, 1)
                schedule = await session.get(ScheduleEntry, executed.schedule_entry_id)
                publication = await session.get(Publication, executed.publication_id)
                assert schedule is not None
                assert publication is not None
                assert schedule.status == "pending"
                assert schedule.repeat_rule == {}
                assert publication.status == "queued"
                assert publication.execution_mode == "canonical"
                assert publication.attempt_count == 0
                assert publication.telegram_message_ids is None
                assert dict(schedule.meta or {})["admin_agent_execution_key"] == executed.execution_key
                assert dict(publication.meta or {})["admin_agent_execution_key"] == executed.execution_key
                assert (
                    await session.execute(
                        select(func.count(PublicationAttempt.id)).where(
                            PublicationAttempt.publication_id == publication.id
                        )
                    )
                ).scalar_one() == 0

                retry = await service.approve(
                    approval_id=approval.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert retry is not None
                assert retry.schedule_entry_id == executed.schedule_entry_id
                assert retry.publication_id == executed.publication_id
                assert len(calls) == 1
                assert await _counts(session, channel.id) == (1, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_edit_or_manual_schedule_before_approve_marks_proposal_stale() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9915)
                first = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="Edited later",
                )
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                service = AdminAgentApprovalService(session, now_utc=now)
                edited_proposal = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=first.id,
                    local_time_value="12:00",
                    request_id="approval-stale-edit-0001",
                )
                await ContentRepo(session).append_revision(
                    first.id,
                    PostDocument(
                        blocks=[{"id": "b2", "type": "text", "text": "Новая revision"}]
                    ),
                    created_by_tg_user_id=owner.tg_user_id,
                    source="studio",
                    status="draft",
                )
                stale = await service.approve(
                    approval_id=edited_proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert stale is not None
                assert stale.state == STATE_STALE
                assert "revision changed" in str(stale.failure_reason)
                assert await _counts(session, channel.id) == (0, 0)

                second = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="Manually scheduled",
                )
                conflict_proposal = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=second.id,
                    local_time_value="13:00",
                    request_id="approval-stale-conflict-0001",
                )
                manual = await LegacyPublicationBridge(session).queue(
                    content_item_id=second.id,
                    content_revision=second.current_revision,
                    scheduled_at=now + timedelta(hours=6),
                    timezone_name="UTC+3",
                )
                stale_conflict = await service.approve(
                    approval_id=conflict_proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert stale_conflict is not None
                assert stale_conflict.state == STATE_STALE
                assert "canonical scheduling state" in str(stale_conflict.failure_reason)
                assert await _counts(session, channel.id) == (1, 1)
                assert stale_conflict.schedule_entry_id is None
                assert stale_conflict.publication_id is None
                assert manual.id is not None

                retry = await service.approve(
                    approval_id=edited_proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert retry is not None and retry.state == STATE_STALE
                assert await _counts(session, channel.id) == (1, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_crash_after_canonical_commit_recovers_same_pair(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9916)
                item = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                )
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                service = AdminAgentApprovalService(session, now_utc=now)
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="18:00",
                    request_id="approval-recovery-0001",
                )
                original_finalize = service._finalize_executed

                async def crash_after_queue(*args, **kwargs):
                    raise RuntimeError("synthetic crash after canonical commit")

                monkeypatch.setattr(service, "_finalize_executed", crash_after_queue)
                with pytest.raises(ApprovalExecutionError, match="canonical scheduling attempt failed"):
                    await service.approve(
                        approval_id=approval.id,
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        reviewer_tg_user_id=owner.tg_user_id,
                    )
                assert await _counts(session, channel.id) == (1, 1)
                persisted = await session.get(AdminAgentApproval, approval.id)
                assert persisted is not None
                assert persisted.state == STATE_EXECUTING
                assert persisted.schedule_entry_id is None
                assert persisted.publication_id is None
                execution_key = persisted.execution_key

                monkeypatch.setattr(service, "_finalize_executed", original_finalize)
                recovered = await service.approve(
                    approval_id=approval.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert recovered is not None
                assert recovered.state == STATE_EXECUTED
                assert recovered.execution_key == execution_key
                assert recovered.schedule_entry_id is not None
                assert recovered.publication_id is not None
                assert await _counts(session, channel.id) == (1, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_overlapping_approve_has_one_execution_winner(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "concurrent-approve.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as setup:
                owner, channel = await _setup_channel(setup, tg_user_id=9917)
                item = await _draft(
                    setup,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                )
                service = AdminAgentApprovalService(
                    setup,
                    now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                )
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="19:00",
                    request_id="approval-concurrent-0001",
                )
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)
                approval_id = int(approval.id)

            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0
            original_queue = LegacyPublicationBridge.queue

            async def blocked_queue(self, **kwargs):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return await original_queue(self, **kwargs)

            monkeypatch.setattr(LegacyPublicationBridge, "queue", blocked_queue)

            async with Session() as first_session:
                first_service = AdminAgentApprovalService(
                    first_session,
                    now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                )
                first_task = asyncio.create_task(
                    first_service.approve(
                        approval_id=approval_id,
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        reviewer_tg_user_id=owner_id,
                    )
                )
                await entered.wait()

                async with Session() as second_session:
                    second_service = AdminAgentApprovalService(
                        second_session,
                        now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                    )
                    overlap = await second_service.approve(
                        approval_id=approval_id,
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        reviewer_tg_user_id=owner_id,
                    )
                    assert overlap is not None
                    assert overlap.state == STATE_EXECUTING
                    assert calls == 1

                release.set()
                winner = await first_task
                assert winner is not None
                assert winner.state == STATE_EXECUTED

            async with Session() as verify:
                assert await _counts(verify, channel_id) == (1, 1)
                stored = await verify.get(AdminAgentApproval, approval_id)
                assert stored is not None
                assert stored.state == STATE_EXECUTED
                assert calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
