from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.admin_agent import (
    AdminAgentApprovalBatch,
    AdminAgentApprovalBatchItem,
    AdminAgentRunArtifact,
)
from app.domain.content import PostDocument
from app.domain.content.models import ContentItem
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.ai_settings import ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.content import ContentRepo
from app.services.admin_agent import AdminAgentRunner, SERIES_SCENARIO_LIMITS
from app.services.admin_agent_series_approvals import (
    ITEM_EXECUTED,
    ITEM_EXECUTING,
    ITEM_PENDING,
    STATE_EXECUTED,
    STATE_EXECUTING,
    STATE_PARTIAL_FAILED,
    STATE_PENDING_REVIEW,
    STATE_REJECTED,
    STATE_STALE,
    AdminAgentSeriesApprovalService,
    SeriesApprovalIdempotencyConflict,
    SeriesApprovalInputError,
)
from app.services.ai_generation import AIGenerationService
from app.services.publication_bridge import LegacyPublicationBridge


NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


def _payload(count: int) -> str:
    return json.dumps(
        {
            "series": {
                "title": "Серия для approval-gated расписания",
                "summary": "Bounded evergreen summary.",
            },
            "posts": [
                {
                    "title": f"Пост {ordinal}",
                    "angle": f"Угол {ordinal}",
                    "objective": f"Цель {ordinal}",
                    "text": f"Evergreen body {ordinal}.",
                }
                for ordinal in range(1, count + 1)
            ],
        },
        ensure_ascii=False,
    )


async def _setup_channel(session, tg_user_id: int):
    owner = await ClientsRepo(session).create_or_get(
        tg_user_id,
        f"series_approval_{tg_user_id}",
        "Series Approval Owner",
    )
    channel = await ChannelsRepo(session).create(
        owner.id,
        -3000000 - tg_user_id,
        "Series approval channel",
    )
    ai = await ChannelAISettingsRepo(session).get_or_create(channel.id)
    ai.enabled = True
    ai.model = "provider/model"
    await session.commit()
    return owner, channel


async def _source_run(
    session,
    monkeypatch,
    *,
    owner,
    channel,
    count: int = 4,
    request_id: str = "series-source-0001",
):
    async def generated(self, **kwargs):
        return {
            "success": True,
            "text": _payload(count),
            "tokens_used": 20,
            "model": "provider/model",
            "error": None,
        }

    monkeypatch.setattr(AIGenerationService, "run_pipeline", generated)
    run = await AdminAgentRunner(
        session,
        limits=SERIES_SCENARIO_LIMITS,
        now_utc=NOW,
    ).run_prepare_content_series(
        channel_id=channel.id,
        owner_tg_user_id=owner.tg_user_id,
        request_id=request_id,
        brief="Подготовь bounded evergreen серию для теста approval scheduling.",
        post_count=count,
    )
    assert run.status == "completed"
    return run


def _slots(count: int, *, minute_offset: int = 0) -> list[dict[str, object]]:
    return [
        {
            "ordinal": ordinal,
            "local_date": "2026-09-20",
            "local_time": f"{12 + ordinal:02d}:{minute_offset:02d}",
        }
        for ordinal in range(1, count + 1)
    ]


async def _counts(session, channel_id: int) -> tuple[int, int, int]:
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
    attempt_count = int(
        (
            await session.execute(
                select(func.count(PublicationAttempt.id)).join(
                    Publication,
                    Publication.id == PublicationAttempt.publication_id,
                ).where(Publication.channel_id == int(channel_id))
            )
        ).scalar_one()
    )
    return schedule_count, publication_count, attempt_count


def test_series_proposal_is_server_authored_snapshot_and_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 13001)
                source = await _source_run(
                    session,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                )
                first_post = source.result["posts"][0]
                await ContentRepo(session).append_revision(
                    first_post["content_item_id"],
                    PostDocument(
                        blocks=[
                            {
                                "id": "edited-before-proposal",
                                "type": "text",
                                "text": "Edited current draft before proposal.",
                            }
                        ]
                    ),
                    created_by_tg_user_id=owner.tg_user_id,
                    source="studio",
                    status="draft",
                )

                service = AdminAgentSeriesApprovalService(session, now_utc=NOW)
                proposal = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source.id,
                    request_id="series-approval-create-0001",
                    slots=_slots(4),
                )
                assert proposal.state == STATE_PENDING_REVIEW
                assert proposal.item_count == 4
                assert proposal.timezone == "UTC+3"
                items = await service.items_for_batch(proposal.id)
                assert [item.ordinal for item in items] == [1, 2, 3, 4]
                assert items[0].captured_content_revision == 2
                assert [item.state for item in items] == [ITEM_PENDING] * 4
                assert len({item.execution_key for item in items}) == 4
                assert all(len(item.item_fingerprint) == 64 for item in items)
                assert await _counts(session, channel.id) == (0, 0, 0)

                await ContentRepo(session).append_revision(
                    first_post["content_item_id"],
                    PostDocument(
                        blocks=[
                            {
                                "id": "edited-after-proposal",
                                "type": "text",
                                "text": "Edited again after proposal.",
                            }
                        ]
                    ),
                    created_by_tg_user_id=owner.tg_user_id,
                    source="studio",
                    status="draft",
                )
                same = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source.id,
                    request_id="series-approval-create-0001",
                    slots=_slots(4),
                )
                assert same.id == proposal.id
                same_items = await service.items_for_batch(same.id)
                assert same_items[0].captured_content_revision == 2

                changed = _slots(4)
                changed[1]["local_time"] = "18:15"
                with pytest.raises(SeriesApprovalIdempotencyConflict):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        source_run_id=source.id,
                        request_id="series-approval-create-0001",
                        slots=changed,
                    )

                malformed_sets = [
                    _slots(3),
                    _slots(4) + [{"ordinal": 5, "local_date": "2026-09-20", "local_time": "19:00"}],
                    [
                        _slots(4)[0],
                        _slots(4)[0],
                        _slots(4)[2],
                        _slots(4)[3],
                    ],
                ]
                for index, bad_slots in enumerate(malformed_sets, start=1):
                    with pytest.raises(SeriesApprovalInputError):
                        await service.create(
                            owner_tg_user_id=owner.tg_user_id,
                            channel_id=channel.id,
                            source_run_id=source.id,
                            request_id=f"series-invalid-set-{index:04d}",
                            slots=bad_slots,
                        )

                duplicate_time = _slots(4)
                duplicate_time[1]["local_time"] = duplicate_time[0]["local_time"]
                with pytest.raises(SeriesApprovalInputError, match="duplicate exact"):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        source_run_id=source.id,
                        request_id="series-invalid-time-0001",
                        slots=duplicate_time,
                    )

                past = _slots(4)
                past[0] = {
                    "ordinal": 1,
                    "local_date": "2026-09-19",
                    "local_time": "10:00",
                }
                with pytest.raises(SeriesApprovalInputError, match="future"):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        source_run_id=source.id,
                        request_id="series-invalid-past-0001",
                        slots=past,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_source_run_and_artifacts_fail_closed(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 13002)
                source = await _source_run(
                    session,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                    request_id="series-source-validation-0001",
                )
                service = AdminAgentSeriesApprovalService(session, now_utc=NOW)

                with pytest.raises(SeriesApprovalInputError, match="not found"):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id + 1,
                        channel_id=channel.id,
                        source_run_id=source.id,
                        request_id="series-foreign-owner-0001",
                        slots=_slots(4),
                    )

                source.scenario = "drafts_tomorrow"
                await session.commit()
                with pytest.raises(SeriesApprovalInputError, match="not a content-series"):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        source_run_id=source.id,
                        request_id="series-wrong-scenario-0001",
                        slots=_slots(4),
                    )
                source.scenario = "prepare_content_series"
                source.status = "failed"
                await session.commit()
                with pytest.raises(SeriesApprovalInputError, match="not completed"):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        source_run_id=source.id,
                        request_id="series-incomplete-source-0001",
                        slots=_slots(4),
                    )
                source.status = "completed"
                await session.commit()

                artifact = (
                    await session.execute(
                        select(AdminAgentRunArtifact)
                        .where(AdminAgentRunArtifact.run_id == source.id)
                        .order_by(AdminAgentRunArtifact.ordinal.desc())
                        .limit(1)
                    )
                ).scalar_one()
                await session.delete(artifact)
                await session.commit()
                with pytest.raises(SeriesApprovalInputError, match="artifacts"):
                    await service.create(
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        source_run_id=source.id,
                        request_id="series-partial-artifact-0001",
                        slots=_slots(4),
                    )
                assert await _counts(session, channel.id) == (0, 0, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_approve_uses_bridge_once_per_item_and_retry_never_duplicates(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 13003)
                source = await _source_run(
                    session,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                    request_id="series-source-execute-0001",
                )
                service = AdminAgentSeriesApprovalService(session, now_utc=NOW)
                proposal = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source.id,
                    request_id="series-execute-0001",
                    slots=_slots(4),
                )

                original_queue = LegacyPublicationBridge.queue
                calls: list[dict] = []

                async def observed_queue(self, **kwargs):
                    calls.append(dict(kwargs))
                    return await original_queue(self, **kwargs)

                monkeypatch.setattr(LegacyPublicationBridge, "queue", observed_queue)
                executed = await service.approve(
                    batch_id=proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert executed is not None
                assert executed.state == STATE_EXECUTED
                assert len(calls) == 4
                assert all(call["repeat_rule"] is None for call in calls)
                assert all(call["runtime_options"] is None for call in calls)
                assert await _counts(session, channel.id) == (4, 4, 0)

                items = await service.items_for_batch(proposal.id)
                assert [item.state for item in items] == [ITEM_EXECUTED] * 4
                assert all(item.schedule_entry_id for item in items)
                assert all(item.publication_id for item in items)
                for item in items:
                    schedule = await session.get(ScheduleEntry, item.schedule_entry_id)
                    publication = await session.get(Publication, item.publication_id)
                    assert schedule is not None and publication is not None
                    required = {
                        "admin_agent_batch_approval_id": proposal.id,
                        "admin_agent_batch_execution_key": proposal.execution_key,
                        "admin_agent_batch_item_id": item.id,
                        "admin_agent_item_execution_key": item.execution_key,
                        "admin_agent_source_run_id": source.id,
                        "admin_agent_series_ordinal": item.ordinal,
                        "admin_agent_batch_fingerprint": proposal.action_fingerprint,
                        "admin_agent_item_fingerprint": item.item_fingerprint,
                    }
                    assert all(schedule.meta[key] == value for key, value in required.items())
                    assert all(
                        publication.meta[key] == value for key, value in required.items()
                    )

                retry = await service.approve(
                    batch_id=proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert retry is not None and retry.state == STATE_EXECUTED
                assert len(calls) == 4
                assert await _counts(session, channel.id) == (4, 4, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_full_preflight_stale_and_reject_have_zero_batch_mutations(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 13004)
                source = await _source_run(
                    session,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                    request_id="series-source-stale-0001",
                )
                service = AdminAgentSeriesApprovalService(session, now_utc=NOW)
                proposal = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source.id,
                    request_id="series-stale-revision-0001",
                    slots=_slots(4),
                )
                items = await service.items_for_batch(proposal.id)
                await ContentRepo(session).append_revision(
                    items[2].content_item_id,
                    PostDocument(
                        blocks=[
                            {
                                "id": "stale-after-proposal",
                                "type": "text",
                                "text": "Changed after proposal.",
                            }
                        ]
                    ),
                    created_by_tg_user_id=owner.tg_user_id,
                    source="studio",
                    status="draft",
                )
                stale = await service.approve(
                    batch_id=proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert stale is not None and stale.state == STATE_STALE
                assert "revision changed" in str(stale.failure_reason)
                assert await _counts(session, channel.id) == (0, 0, 0)

                source2 = await _source_run(
                    session,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                    count=2,
                    request_id="series-source-reject-0001",
                )
                rejected_proposal = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source2.id,
                    request_id="series-reject-0001",
                    slots=_slots(2, minute_offset=10),
                )
                rejected = await service.reject(
                    batch_id=rejected_proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert rejected is not None and rejected.state == STATE_REJECTED
                assert await _counts(session, channel.id) == (0, 0, 0)
                with pytest.raises(Exception):
                    await service.reject(
                        batch_id=proposal.id,
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        reviewer_tg_user_id=owner.tg_user_id,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_crash_recovery_and_partial_failure_are_no_replay(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                owner, channel = await _setup_channel(session, 13005)
                source = await _source_run(
                    session,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                    count=3,
                    request_id="series-source-recovery-0001",
                )
                service = AdminAgentSeriesApprovalService(session, now_utc=NOW)
                proposal = await service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source.id,
                    request_id="series-recovery-0001",
                    slots=_slots(3),
                )
                original_finalize = service._finalize_item

                async def crash_after_bridge(*args, **kwargs):
                    raise RuntimeError("synthetic crash after canonical commit")

                monkeypatch.setattr(service, "_finalize_item", crash_after_bridge)
                with pytest.raises(RuntimeError, match="synthetic crash"):
                    await service.approve(
                        batch_id=proposal.id,
                        owner_tg_user_id=owner.tg_user_id,
                        channel_id=channel.id,
                        reviewer_tg_user_id=owner.tg_user_id,
                    )
                assert await _counts(session, channel.id) == (1, 1, 0)
                stored = await session.get(AdminAgentApprovalBatch, proposal.id)
                assert stored is not None and stored.state == STATE_EXECUTING
                stored_items = await service.items_for_batch(proposal.id)
                assert stored_items[0].state == ITEM_EXECUTING
                assert stored_items[0].schedule_entry_id is None

                monkeypatch.setattr(service, "_finalize_item", original_finalize)
                recovery_service = AdminAgentSeriesApprovalService(
                    session,
                    now_utc=NOW + timedelta(seconds=31),
                )
                recovered = await recovery_service.approve(
                    batch_id=proposal.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert recovered is not None and recovered.state == STATE_EXECUTED
                assert await _counts(session, channel.id) == (3, 3, 0)

                source2 = await _source_run(
                    session,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                    count=3,
                    request_id="series-source-partial-0001",
                )
                partial_service = AdminAgentSeriesApprovalService(
                    session,
                    now_utc=NOW,
                )
                partial = await partial_service.create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source2.id,
                    request_id="series-partial-0001",
                    slots=_slots(3, minute_offset=20),
                )
                partial_items = await partial_service.items_for_batch(partial.id)
                original_partial_finalize = partial_service._finalize_item
                edited = False

                async def finalize_then_edit(*, item, schedule, publication):
                    nonlocal edited
                    await original_partial_finalize(
                        item=item,
                        schedule=schedule,
                        publication=publication,
                    )
                    if item.ordinal == 1 and not edited:
                        edited = True
                        await ContentRepo(session).append_revision(
                            partial_items[1].content_item_id,
                            PostDocument(
                                blocks=[
                                    {
                                        "id": "race-edit",
                                        "type": "text",
                                        "text": "Changed during bounded execution.",
                                    }
                                ]
                            ),
                            created_by_tg_user_id=owner.tg_user_id,
                            source="studio",
                            status="draft",
                        )

                monkeypatch.setattr(
                    partial_service,
                    "_finalize_item",
                    finalize_then_edit,
                )
                partial_result = await partial_service.approve(
                    batch_id=partial.id,
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    reviewer_tg_user_id=owner.tg_user_id,
                )
                assert partial_result is not None
                assert partial_result.state == STATE_PARTIAL_FAILED
                final_items = await partial_service.items_for_batch(partial.id)
                assert final_items[0].state == ITEM_EXECUTED
                assert final_items[0].schedule_entry_id is not None
                assert final_items[1].state == "stale"
                assert final_items[2].state == ITEM_PENDING
                assert await _counts(session, channel.id) == (4, 4, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_concurrent_approve_has_single_batch_winner(
    monkeypatch,
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "series-concurrent.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as setup:
                owner, channel = await _setup_channel(setup, 13006)
                source = await _source_run(
                    setup,
                    monkeypatch,
                    owner=owner,
                    channel=channel,
                    count=2,
                    request_id="series-source-concurrent-0001",
                )
                proposal = await AdminAgentSeriesApprovalService(
                    setup,
                    now_utc=NOW,
                ).create(
                    owner_tg_user_id=owner.tg_user_id,
                    channel_id=channel.id,
                    source_run_id=source.id,
                    request_id="series-concurrent-0001",
                    slots=_slots(2),
                )
                owner_id = int(owner.tg_user_id)
                channel_id = int(channel.id)
                batch_id = int(proposal.id)

            preflight_barrier = asyncio.Event()
            queue_entered = asyncio.Event()
            release_queue = asyncio.Event()
            arrivals = 0
            queue_calls = 0
            original_preflight = AdminAgentSeriesApprovalService._full_preflight
            original_queue = LegacyPublicationBridge.queue

            async def synchronized_preflight(self, batch, items):
                nonlocal arrivals
                if batch.state == STATE_PENDING_REVIEW:
                    arrivals += 1
                    if arrivals == 2:
                        preflight_barrier.set()
                    await asyncio.wait_for(preflight_barrier.wait(), timeout=5)
                return await original_preflight(self, batch, items)

            async def blocked_queue(self, **kwargs):
                nonlocal queue_calls
                queue_calls += 1
                if queue_calls == 1:
                    queue_entered.set()
                    await asyncio.wait_for(release_queue.wait(), timeout=5)
                return await original_queue(self, **kwargs)

            monkeypatch.setattr(
                AdminAgentSeriesApprovalService,
                "_full_preflight",
                synchronized_preflight,
            )
            monkeypatch.setattr(LegacyPublicationBridge, "queue", blocked_queue)

            async with Session() as first_session, Session() as second_session:
                first_service = AdminAgentSeriesApprovalService(
                    first_session,
                    now_utc=NOW,
                )
                second_service = AdminAgentSeriesApprovalService(
                    second_session,
                    now_utc=NOW,
                )
                first_task = asyncio.create_task(
                    first_service.approve(
                        batch_id=batch_id,
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        reviewer_tg_user_id=owner_id,
                    )
                )
                second_task = asyncio.create_task(
                    second_service.approve(
                        batch_id=batch_id,
                        owner_tg_user_id=owner_id,
                        channel_id=channel_id,
                        reviewer_tg_user_id=owner_id,
                    )
                )
                await asyncio.wait_for(queue_entered.wait(), timeout=5)
                assert arrivals == 2
                assert queue_calls == 1
                release_queue.set()
                first_result, second_result = await asyncio.wait_for(
                    asyncio.gather(first_task, second_task),
                    timeout=5,
                )
                assert first_result is not None and second_result is not None
                assert {first_result.state, second_result.state} <= {
                    STATE_EXECUTING,
                    STATE_EXECUTED,
                }

            async with Session() as verify:
                assert await _counts(verify, channel_id) == (2, 2, 0)
                stored = await verify.get(AdminAgentApprovalBatch, batch_id)
                assert stored is not None and stored.state == STATE_EXECUTED
                assert queue_calls == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
