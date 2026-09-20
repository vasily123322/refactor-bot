from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.admin_agent_approvals as approval_service_module
from app.core.db import Base
from app.core.timezone import localize_wall_clock_strict
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
    ApprovalStateConflict,
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


def test_strict_wall_clock_rejects_dst_gap_and_overlap() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        localize_wall_clock_strict(
            datetime(2026, 3, 29, 2, 30),
            "Europe/Berlin",
        )
    with pytest.raises(ValueError, match="ambiguous"):
        localize_wall_clock_strict(
            datetime(2026, 10, 25, 2, 30),
            "Europe/Berlin",
        )

    normal = localize_wall_clock_strict(
        datetime(2026, 3, 29, 3, 30),
        "Europe/Berlin",
    )
    assert normal.astimezone(timezone.utc) == datetime(
        2026,
        3,
        29,
        1,
        30,
        tzinfo=timezone.utc,
    )

    fixed = localize_wall_clock_strict(
        datetime(2026, 3, 29, 2, 30),
        "UTC+3",
    )
    assert fixed.astimezone(timezone.utc) == datetime(
        2026,
        3,
        28,
        23,
        30,
        tzinfo=timezone.utc,
    )


def test_draft_approval_dst_invalid_wall_clock_fails_closed() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9921)
                anchor_item = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="DST timezone anchor",
                )
                session.add(
                    ScheduleEntry(
                        content_item_id=anchor_item.id,
                        content_revision=anchor_item.current_revision,
                        channel_id=channel.id,
                        scheduled_at=datetime(2026, 3, 28, 14, 0, tzinfo=timezone.utc),
                        timezone="Europe/Berlin",
                        status="completed",
                        repeat_rule={},
                        meta={},
                    )
                )
                await session.commit()

                gap_item = await _draft(
                    session,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="DST gap",
                )
                gap_service = AdminAgentApprovalService(
                    session,
                    now_utc=datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc),
                )
                with pytest.raises(ApprovalInputError, match="does not exist"):
                    await gap_service.create_schedule_draft_tomorrow(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        content_item_id=gap_item.id,
                        local_time_value="02:30",
                        request_id="approval-dst-gap-0001",
                    )
                assert (
                    await session.execute(
                        select(func.count(AdminAgentApproval.id)).where(
                            AdminAgentApproval.channel_id == channel.id
                        )
                    )
                ).scalar_one() == 0

                valid = await gap_service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=gap_item.id,
                    local_time_value="03:30",
                    request_id="approval-dst-revalidate-0001",
                )
                valid.local_time = "02:30"
                await session.commit()
                stale = await gap_service.approve(
                    approval_id=valid.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert stale is not None
                assert stale.state == STATE_STALE
                assert stale.failure_reason == "local time does not exist in timezone"

                overlap_owner, overlap_channel = await _setup_channel(
                    session,
                    tg_user_id=9922,
                )
                overlap_anchor = await _draft(
                    session,
                    channel_id=overlap_channel.id,
                    owner_tg_user_id=overlap_owner.tg_user_id,
                    title="DST overlap timezone anchor",
                )
                session.add(
                    ScheduleEntry(
                        content_item_id=overlap_anchor.id,
                        content_revision=overlap_anchor.current_revision,
                        channel_id=overlap_channel.id,
                        scheduled_at=datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc),
                        timezone="Europe/Berlin",
                        status="completed",
                        repeat_rule={},
                        meta={},
                    )
                )
                await session.commit()
                overlap_item = await _draft(
                    session,
                    channel_id=overlap_channel.id,
                    owner_tg_user_id=overlap_owner.tg_user_id,
                    title="DST overlap",
                )
                overlap_service = AdminAgentApprovalService(
                    session,
                    now_utc=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc),
                )
                with pytest.raises(ApprovalInputError, match="ambiguous"):
                    await overlap_service.create_schedule_draft_tomorrow(
                        owner_tg_user_id=overlap_owner.tg_user_id,
                        channel_id=overlap_channel.id,
                        content_item_id=overlap_item.id,
                        local_time_value="02:30",
                        request_id="approval-dst-overlap-0001",
                    )
                assert (
                    await session.execute(
                        select(func.count(AdminAgentApproval.id)).where(
                            AdminAgentApproval.channel_id == overlap_channel.id
                        )
                    )
                ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_one_active_draft_approval_per_revision_and_terminal_reuse() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, tg_user_id=9923)
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)
                item = await _draft(
                    session,
                    channel_id=channel_id,
                    owner_tg_user_id=owner_id,
                    title="Active approval target",
                )
                item_id = int(item.id)
                service = AdminAgentApprovalService(
                    session,
                    now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                )
                revision_one = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    content_item_id=item_id,
                    local_time_value="14:00",
                    request_id="active-single-r1-0001",
                )
                assert revision_one.content_revision == 1

                with pytest.raises(ApprovalStateConflict, match="active approval"):
                    await service.create_schedule_draft_tomorrow(
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        content_item_id=item_id,
                        local_time_value="14:30",
                        request_id="active-single-r1-0002",
                    )

                await ContentRepo(session).append_revision(
                    item_id,
                    PostDocument(
                        blocks=[
                            {
                                "id": "active-r2",
                                "type": "text",
                                "text": "Revision two remains independently reviewable.",
                            }
                        ]
                    ),
                    created_by_tg_user_id=owner_id,
                    source="studio",
                    status="draft",
                )
                revision_two = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    content_item_id=item_id,
                    local_time_value="15:00",
                    request_id="active-single-r2-0001",
                )
                assert revision_two.content_revision == 2
                revision_two_id = int(revision_two.id)

                with pytest.raises(ApprovalStateConflict, match="active approval"):
                    await service.create_schedule_draft_tomorrow(
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        content_item_id=item_id,
                        local_time_value="15:30",
                        request_id="active-single-r2-0002",
                    )

                rejected = await service.reject(
                    approval_id=revision_two_id,
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    reviewer_tg_user_id=owner_id,
                )
                assert rejected is not None and rejected.state == STATE_REJECTED

                fresh = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    content_item_id=item_id,
                    local_time_value="16:00",
                    request_id="active-single-r2-0003",
                )
                assert fresh.id != revision_two_id
                assert fresh.content_revision == 2
                assert fresh.state == STATE_PENDING_REVIEW
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_draft_proposals_have_one_active_winner(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "concurrent-draft-proposal.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as setup:
                owner, channel = await _setup_channel(setup, tg_user_id=9924)
                item = await _draft(
                    setup,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="Concurrent active target",
                )
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)
                item_id = int(item.id)

            async with Session() as first_session, Session() as second_session:
                first_service = AdminAgentApprovalService(
                    first_session,
                    now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                )
                second_service = AdminAgentApprovalService(
                    second_session,
                    now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                )
                results = await asyncio.wait_for(
                    asyncio.gather(
                        first_service.create_schedule_draft_tomorrow(
                            owner_tg_user_id=owner_id,
                            channel_id=channel_id,
                            content_item_id=item_id,
                            local_time_value="17:00",
                            request_id="active-single-concurrent-a",
                        ),
                        second_service.create_schedule_draft_tomorrow(
                            owner_tg_user_id=owner_id,
                            channel_id=channel_id,
                            content_item_id=item_id,
                            local_time_value="17:30",
                            request_id="active-single-concurrent-b",
                        ),
                        return_exceptions=True,
                    ),
                    timeout=5,
                )

            winners = [row for row in results if isinstance(row, AdminAgentApproval)]
            conflicts = [row for row in results if isinstance(row, ApprovalStateConflict)]
            assert len(winners) == 1
            assert len(conflicts) == 1
            async with Session() as verify:
                active = list(
                    (
                        await verify.execute(
                            select(AdminAgentApproval).where(
                                AdminAgentApproval.channel_id == channel_id,
                                AdminAgentApproval.content_item_id == item_id,
                                AdminAgentApproval.content_revision == 1,
                                AdminAgentApproval.state.in_(
                                    [STATE_PENDING_REVIEW, STATE_EXECUTING]
                                ),
                            )
                        )
                    ).scalars()
                )
                assert len(active) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


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
                source_run.result = {
                    "scenario": "drafts_tomorrow",
                    "drafts": [
                        {
                            "content_item_id": item.id,
                            "content_revision": item.current_revision,
                        }
                    ],
                }
                await session.commit()
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
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)
                item = await _draft(
                    session,
                    channel_id=channel_id,
                    owner_tg_user_id=owner_id,
                )
                item_id = int(item.id)
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                service = AdminAgentApprovalService(session, now_utc=now)
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    content_item_id=item_id,
                    local_time_value="17:15",
                    request_id="approval-execute-0001",
                )
                approval_id = int(approval.id)

                original_queue = LegacyPublicationBridge.queue
                calls: list[dict] = []

                async def tracked_queue(self, **kwargs):
                    calls.append(dict(kwargs))
                    return await original_queue(self, **kwargs)

                monkeypatch.setattr(LegacyPublicationBridge, "queue", tracked_queue)
                executed = await service.approve(
                    approval_id=approval_id,
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    reviewer_tg_user_id=owner_id,
                )
                assert executed is not None
                assert executed.state == STATE_EXECUTED
                assert len(calls) == 1
                assert calls[0]["content_item_id"] == item_id
                assert calls[0]["content_revision"] == 1
                assert calls[0]["repeat_rule"] is None
                assert calls[0]["runtime_options"] is None
                assert calls[0]["metadata"]["admin_agent_approval_id"] == approval_id
                assert calls[0]["metadata"]["admin_agent_execution_key"] == executed.execution_key

                assert await _counts(session, channel_id) == (1, 1)
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
                    approval_id=approval_id,
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    reviewer_tg_user_id=owner_id,
                )
                assert retry is not None
                assert retry.schedule_entry_id == executed.schedule_entry_id
                assert retry.publication_id == executed.publication_id
                assert len(calls) == 1
                assert await _counts(session, channel_id) == (1, 1)
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
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)
                item = await _draft(
                    session,
                    channel_id=channel_id,
                    owner_tg_user_id=owner_id,
                )
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                service = AdminAgentApprovalService(session, now_utc=now)
                approval = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    content_item_id=item.id,
                    local_time_value="18:00",
                    request_id="approval-recovery-0001",
                )
                approval_id = int(approval.id)

                original_finalize = service._finalize_executed

                async def crash_after_queue(*args, **kwargs):
                    raise RuntimeError("synthetic crash after canonical commit")

                monkeypatch.setattr(service, "_finalize_executed", crash_after_queue)
                with pytest.raises(ApprovalExecutionError, match="canonical scheduling attempt failed"):
                    await service.approve(
                        approval_id=approval_id,
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        reviewer_tg_user_id=owner_id,
                    )
                assert await _counts(session, channel_id) == (1, 1)
                persisted = await session.get(AdminAgentApproval, approval_id)
                assert persisted is not None
                assert persisted.state == STATE_EXECUTING
                assert persisted.schedule_entry_id is None
                assert persisted.publication_id is None
                execution_key = persisted.execution_key

                monkeypatch.setattr(service, "_finalize_executed", original_finalize)
                recovered = await service.approve(
                    approval_id=approval_id,
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    reviewer_tg_user_id=owner_id,
                )
                assert recovered is not None
                assert recovered.state == STATE_EXECUTED
                assert recovered.execution_key == execution_key
                assert recovered.schedule_entry_id is not None
                assert recovered.publication_id is not None
                assert await _counts(session, channel_id) == (1, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_simultaneous_approve_has_one_atomic_execution_winner(
    monkeypatch,
    tmp_path,
) -> None:
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

            review_barrier = asyncio.Event()
            queue_entered = asyncio.Event()
            release_queue = asyncio.Event()
            review_arrivals = 0
            queue_calls = 0

            original_stale_reason = AdminAgentApprovalService._stale_reason
            original_queue = LegacyPublicationBridge.queue

            async def synchronized_stale_reason(self, approval):
                nonlocal review_arrivals
                if approval.state == STATE_PENDING_REVIEW:
                    review_arrivals += 1
                    if review_arrivals == 2:
                        review_barrier.set()
                    await asyncio.wait_for(review_barrier.wait(), timeout=5)
                return await original_stale_reason(self, approval)

            async def blocked_queue(self, **kwargs):
                nonlocal queue_calls
                queue_calls += 1
                queue_entered.set()
                await asyncio.wait_for(release_queue.wait(), timeout=5)
                return await original_queue(self, **kwargs)

            monkeypatch.setattr(
                AdminAgentApprovalService,
                "_stale_reason",
                synchronized_stale_reason,
            )
            monkeypatch.setattr(LegacyPublicationBridge, "queue", blocked_queue)

            async with Session() as first_session:
                async with Session() as second_session:
                    first_service = AdminAgentApprovalService(
                        first_session,
                        now_utc=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc),
                    )
                    second_service = AdminAgentApprovalService(
                        second_session,
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
                    second_task = asyncio.create_task(
                        second_service.approve(
                            approval_id=approval_id,
                            owner_tg_user_id=owner_id,
                            channel_id=channel_id,
                            reviewer_tg_user_id=owner_id,
                        )
                    )

                    await asyncio.wait_for(queue_entered.wait(), timeout=5)
                    assert review_arrivals == 2
                    assert queue_calls == 1
                    release_queue.set()
                    first_result, second_result = await asyncio.wait_for(
                        asyncio.gather(first_task, second_task),
                        timeout=5,
                    )
                    assert first_result is not None
                    assert second_result is not None
                    assert {
                        first_result.state,
                        second_result.state,
                    } <= {STATE_EXECUTING, STATE_EXECUTED}

            async with Session() as verify:
                assert await _counts(verify, channel_id) == (1, 1)
                stored = await verify.get(AdminAgentApproval, approval_id)
                assert stored is not None
                assert stored.state == STATE_EXECUTED
                assert queue_calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())



def test_request_id_scope_does_not_leak_and_ineligible_draft_goes_stale() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                first_owner, first_channel = await _setup_channel(
                    session,
                    tg_user_id=9918,
                )
                second_owner, second_channel = await _setup_channel(
                    session,
                    tg_user_id=9919,
                )
                first_item = await _draft(
                    session,
                    channel_id=first_channel.id,
                    owner_tg_user_id=first_owner.tg_user_id,
                )
                second_item = await _draft(
                    session,
                    channel_id=second_channel.id,
                    owner_tg_user_id=second_owner.tg_user_id,
                )
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                service = AdminAgentApprovalService(session, now_utc=now)

                first = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=first_owner.tg_user_id,
                    channel_id=first_channel.id,
                    content_item_id=first_item.id,
                    local_time_value="12:00",
                    request_id="approval-shared-scope-0001",
                )
                second = await service.create_schedule_draft_tomorrow(
                    owner_tg_user_id=second_owner.tg_user_id,
                    channel_id=second_channel.id,
                    content_item_id=second_item.id,
                    local_time_value="13:00",
                    request_id="approval-shared-scope-0001",
                )
                assert first.id != second.id
                assert first.owner_tg_user_id != second.owner_tg_user_id
                assert first.channel_id != second.channel_id
                assert first.content_item_id == first_item.id
                assert second.content_item_id == second_item.id

                await ContentRepo(session).set_status(first_item.id, "archived")
                stale = await service.approve(
                    approval_id=first.id,
                    owner_tg_user_id=first_owner.tg_user_id,
                    channel_id=first_channel.id,
                    reviewer_tg_user_id=first_owner.tg_user_id,
                )
                assert stale is not None
                assert stale.state == STATE_STALE
                assert "no longer eligible" in str(stale.failure_reason)
                assert await _counts(session, first_channel.id) == (0, 0)
                assert await _counts(session, second_channel.id) == (0, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_claim_revalidation_refreshes_cross_session_content_revision(
    monkeypatch,
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "single-content-refresh.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as setup:
                owner, channel = await _setup_channel(setup, tg_user_id=9927)
                item = await _draft(
                    setup,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="Post-claim content refresh",
                )
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                proposal = await AdminAgentApprovalService(
                    setup,
                    now_utc=now,
                ).create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="19:45",
                    request_id="approval-content-refresh-0001",
                )
                approval_id = int(proposal.id)
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)
                content_item_id = int(item.id)
                captured_revision = int(proposal.content_revision)

            durable_claim_loaded = asyncio.Event()
            revision_edited = asyncio.Event()
            queue_calls = 0
            original_load = AdminAgentApprovalService._load
            original_queue = LegacyPublicationBridge.queue

            async def gate_post_claim_load(self, **kwargs):
                loaded = await original_load(self, **kwargs)
                if (
                    loaded is not None
                    and loaded.state == STATE_EXECUTING
                    and loaded.execution_claim_token
                    and not durable_claim_loaded.is_set()
                ):
                    # The execution claim was committed before approve() reaches
                    # this load. End this read transaction while deliberately
                    # retaining the previously cached ContentItem.
                    await self.session.commit()
                    durable_claim_loaded.set()
                    await asyncio.wait_for(revision_edited.wait(), timeout=5)
                return loaded

            async def counted_queue(self, **kwargs):
                nonlocal queue_calls
                queue_calls += 1
                return await original_queue(self, **kwargs)

            monkeypatch.setattr(
                AdminAgentApprovalService,
                "_load",
                gate_post_claim_load,
            )
            monkeypatch.setattr(LegacyPublicationBridge, "queue", counted_queue)

            async with Session() as approval_session, Session() as edit_session:
                approval_task = asyncio.create_task(
                    AdminAgentApprovalService(
                        approval_session,
                        now_utc=now,
                    ).approve(
                        approval_id=approval_id,
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        reviewer_tg_user_id=owner_id,
                    )
                )
                await asyncio.wait_for(durable_claim_loaded.wait(), timeout=5)

                await ContentRepo(edit_session).append_revision(
                    content_item_id,
                    PostDocument(
                        blocks=[
                            {
                                "id": "cross-session-edit",
                                "type": "text",
                                "text": "Changed after initial approval validation.",
                            }
                        ]
                    ),
                    created_by_tg_user_id=owner_id,
                    source="studio",
                    status="draft",
                )
                revision_edited.set()

                result = await asyncio.wait_for(approval_task, timeout=5)
                assert result is not None
                assert result.state == STATE_STALE
                assert result.failure_reason == "draft revision changed"
                assert result.schedule_entry_id is None
                assert result.publication_id is None

            async with Session() as verify:
                stored = await verify.get(AdminAgentApproval, approval_id)
                assert stored is not None
                assert stored.state == STATE_STALE
                assert stored.execution_claim_token is None
                assert stored.schedule_entry_id is None
                assert stored.publication_id is None
                current_item = await verify.get(
                    approval_service_module.ContentItem,
                    content_item_id,
                )
                assert current_item is not None
                assert int(current_item.current_revision) == captured_revision + 1
                assert await _counts(verify, channel_id) == (0, 0)

            assert durable_claim_loaded.is_set()
            assert queue_calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_claim_owner_is_fenced_from_canonical_queue(
    monkeypatch,
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "expired-single-claim.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as setup:
                owner, channel = await _setup_channel(setup, tg_user_id=9926)
                item = await _draft(
                    setup,
                    channel_id=channel.id,
                    owner_tg_user_id=owner.tg_user_id,
                    title="Expired claim fence",
                )
                now = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
                proposal = await AdminAgentApprovalService(
                    setup,
                    now_utc=now,
                ).create_schedule_draft_tomorrow(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    content_item_id=item.id,
                    local_time_value="19:30",
                    request_id="approval-expired-claim-0001",
                )
                approval_id = int(proposal.id)
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)

            fence_entered = asyncio.Event()
            release_old_owner = asyncio.Event()
            fence_calls = 0
            queue_calls = 0
            original_fence = approval_service_module.fence_execution_claim
            original_queue = LegacyPublicationBridge.queue

            async def blocked_first_fence(session, **kwargs):
                nonlocal fence_calls
                fence_calls += 1
                if fence_calls == 1:
                    fence_entered.set()
                    await asyncio.wait_for(release_old_owner.wait(), timeout=5)
                return await original_fence(session, **kwargs)

            async def counted_queue(self, **kwargs):
                nonlocal queue_calls
                queue_calls += 1
                return await original_queue(self, **kwargs)

            monkeypatch.setattr(
                approval_service_module,
                "fence_execution_claim",
                blocked_first_fence,
            )
            monkeypatch.setattr(LegacyPublicationBridge, "queue", counted_queue)

            async with Session() as old_session, Session() as takeover_session:
                old_service = AdminAgentApprovalService(old_session, now_utc=now)
                old_task = asyncio.create_task(
                    old_service.approve(
                        approval_id=approval_id,
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        reviewer_tg_user_id=owner_id,
                    )
                )
                await asyncio.wait_for(fence_entered.wait(), timeout=5)

                takeover_service = AdminAgentApprovalService(
                    takeover_session,
                    now_utc=now + timedelta(seconds=31),
                )
                takeover = await takeover_service.approve(
                    approval_id=approval_id,
                    owner_tg_user_id=owner_id,
                    channel_id=channel_id,
                    reviewer_tg_user_id=owner_id,
                )
                assert takeover is not None
                assert takeover.state == STATE_EXECUTED

                release_old_owner.set()
                old_result = await asyncio.wait_for(old_task, timeout=5)
                assert old_result is not None
                assert old_result.state == STATE_EXECUTED

            async with Session() as verify:
                stored = await verify.get(AdminAgentApproval, approval_id)
                assert stored is not None
                assert stored.state == STATE_EXECUTED
                assert stored.execution_claim_token is None
                assert await _counts(verify, channel_id) == (1, 1)
            assert queue_calls == 1
            assert fence_calls >= 3
        finally:
            await engine.dispose()

    asyncio.run(run())
