from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.content.models import ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter
from app.services.canonical_scheduler_admission import (
    CanonicalSchedulerAdmissionKind,
    CanonicalSchedulerAdmissionService,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.posting import PostingService
from app.services.publication_bridge import LegacyPublicationBridge, PublicationBridgeError
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
    UnsupportedSchedulingProfileError,
)


class _Bot:
    pass


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_channel(Session, suffix: int) -> tuple[Client, Channel]:
    async with Session() as session:
        owner = Client(
            tg_user_id=8_508_000 + suffix,
            username=f"boundary-{suffix}",
            full_name=f"Boundary {suffix}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(8_509_000 + suffix),
            title=f"Boundary {suffix}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        await session.refresh(owner)
        await session.refresh(channel)
        return owner, channel


async def _counts(session) -> tuple[int, int, int, int]:
    schedules = int(
        (await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one()
    )
    publications = int(
        (await session.execute(select(func.count(Publication.id)))).scalar_one()
    )
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    revisions = int(
        (await session.execute(select(func.count(ContentRevision.id)))).scalar_one()
    )
    return schedules, publications, tasks, revisions


@pytest.mark.parametrize(
    "runtime_options",
    [
        {"autodelete_seconds": 600, "autodelete_report": True},
        {"autodelete_views": 100, "autodelete_report": True},
    ],
    ids=["time-report", "views-report"],
)
def test_posting_supported_report_profiles_persist_only_canonical_owner(
    runtime_options,
) -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _owner, channel = await _seed_channel(Session, 1)
            result = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {
                    "type": "text",
                    "text": "Supported report boundary",
                    **runtime_options,
                },
                datetime.now(timezone.utc) + timedelta(minutes=30),
                dedupe_key=f"boundary-report-{sorted(runtime_options)}",
            )
            assert isinstance(result, Publication)

            async with Session() as session:
                persisted = await session.get(Publication, int(result.id))
                assert persisted is not None
                assert persisted.execution_mode == CANONICAL_EXECUTION_MODE
                assert persisted.legacy_post_task_id is None
                assert dict(persisted.meta or {}).get("runtime_options") == runtime_options
                schedules, publications, tasks, _revisions = await _counts(session)
                assert (schedules, publications, tasks) == (1, 1, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "text",
            "text": "Report without trigger",
            "autodelete_report": True,
        },
        {
            "type": "text",
            "text": "Malformed views",
            "autodelete_views": "100",
        },
        {
            "type": "unsupported_boundary_fixture",
            "text": "Unsupported fresh content",
        },
        {
            "type": "text",
            "text": "Fresh request with legacy provenance",
            "_publication_id": 123,
        },
    ],
    ids=[
        "report-only",
        "malformed-runtime",
        "unsupported-content",
        "legacy-provenance",
    ],
)
def test_posting_reject_rolls_back_without_canonical_or_legacy_owner(payload) -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _owner, channel = await _seed_channel(Session, 2)
            with pytest.raises(UnsupportedSchedulingProfileError):
                await PostingService(_Bot(), Session).schedule(
                    int(channel.id),
                    payload,
                    datetime.now(timezone.utc) + timedelta(minutes=10),
                    dedupe_key="boundary-reject",
                )

            async with Session() as session:
                assert await _counts(session) == (0, 0, 0, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("marker", "value"),
    [
        ("_publication_id", 123),
        ("_content_item_id", 456),
        ("_content_revision", 2),
        ("_content_channel_id", 789),
        ("repeat_group_id", 987),
    ],
    ids=[
        "publication-id",
        "content-item-id",
        "content-revision",
        "content-channel-id",
        "repeat-group-id",
    ],
)
def test_posting_mixed_time_views_rejects_historical_provenance_before_legacy_allowlist(
    marker,
    value,
) -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _owner, channel = await _seed_channel(Session, 8)
            payload = {
                "type": "text",
                "text": "Mixed fresh request with historical provenance",
                "autodelete_seconds": 600,
                "autodelete_views": 100,
                marker: value,
            }
            with pytest.raises(UnsupportedSchedulingProfileError):
                await PostingService(_Bot(), Session).schedule(
                    int(channel.id),
                    payload,
                    datetime.now(timezone.utc) + timedelta(minutes=10),
                    dedupe_key=f"boundary-mixed-provenance-{marker}",
                )

            async with Session() as session:
                assert await _counts(session) == (0, 0, 0, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_posting_mixed_positive_time_views_is_only_fresh_posttask_allowlist() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _owner, channel = await _seed_channel(Session, 3)
            result = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {
                    "type": "text",
                    "text": "Mixed retained legacy",
                    "autodelete_seconds": 600,
                    "autodelete_views": 100,
                    "autodelete_report": True,
                },
                datetime.now(timezone.utc) + timedelta(minutes=20),
                dedupe_key="boundary-mixed-allowlist",
            )
            assert isinstance(result, PostTask)

            async with Session() as session:
                task = await session.get(PostTask, int(result.id))
                assert task is not None
                linked = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(task.id)
                        )
                    )
                ).scalar_one()
                assert linked.execution_mode == INTENTIONAL_LEGACY_EXECUTION_MODE
                schedules, publications, tasks, _revisions = await _counts(session)
                assert (schedules, publications, tasks) == (1, 1, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_bridge_supported_report_is_canonical_and_posttask_free() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _seed_channel(Session, 4)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[
                            {
                                "id": "b1",
                                "type": "text",
                                "text": "Bridge supported report",
                            }
                        ]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=25),
                    runtime_options={
                        "autodelete_seconds": 600,
                        "autodelete_report": True,
                    },
                )
                assert publication.execution_mode == CANONICAL_EXECUTION_MODE
                assert publication.legacy_post_task_id is None
                schedules, publications, tasks, _revisions = await _counts(session)
                assert (schedules, publications, tasks) == (1, 1, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_publication_bridge_reject_leaves_no_partial_schedule_or_transport() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _seed_channel(Session, 5)
            async with Session() as session:
                item = await ContentRepo(session).create(
                    channel_id=int(channel.id),
                    document=PostDocument(
                        blocks=[
                            {
                                "id": "b1",
                                "type": "text",
                                "text": "Bridge rejected report",
                            }
                        ]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                with pytest.raises(PublicationBridgeError):
                    await LegacyPublicationBridge(session).queue(
                        content_item_id=int(item.id),
                        scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=25),
                        runtime_options={"autodelete_report": True},
                    )
                schedules, publications, tasks, revisions = await _counts(session)
                assert (schedules, publications, tasks, revisions) == (0, 0, 0, 1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fresh_canonical_repeat_report_continues_without_posttask() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _owner, channel = await _seed_channel(Session, 6)
            source_at = datetime.now(timezone.utc) + timedelta(hours=1)
            result = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {
                    "type": "text",
                    "text": "Fresh canonical repeat report",
                    "repeat_on": True,
                    "repeat_seconds": 3600,
                    "autodelete_seconds": 600,
                    "autodelete_report": True,
                },
                source_at,
                dedupe_key="boundary-repeat-report",
            )
            assert isinstance(result, Publication)
            root_id = int(result.id)

            async with Session() as session:
                root = await session.get(Publication, root_id)
                assert root is not None
                schedule = await session.get(ScheduleEntry, int(root.schedule_entry_id or 0))
                assert schedule is not None
                assert root.legacy_post_task_id is None
                assert dict(root.meta or {}).get("repeat_group_id") == root_id
                assert dict(root.meta or {}).get("runtime_options") == {
                    "autodelete_seconds": 600,
                    "autodelete_report": True,
                }
                schedules, publications, tasks, _revisions = await _counts(session)
                assert (schedules, publications, tasks) == (1, 1, 0)

                root.status = "published"
                root.attempt_count = 1
                root.telegram_message_ids = [8509006]
                schedule.status = "completed"
                session.add(
                    PublicationAttempt(
                        publication_id=root_id,
                        attempt=1,
                        status="published",
                        telegram_message_ids=[8509006],
                        error=None,
                        meta={"canonical_delivery": True},
                        finished_at=source_at,
                    )
                )
                await session.commit()

                reserved = await CanonicalRepeatPlanReservationService(session).reserve_next(
                    root_id,
                    after=source_at,
                )
                assert reserved.outcome == "reserved"
                assert reserved.plan is not None

                successor = await CanonicalRepeatTransportAdapter(session).materialize(root_id)
                assert successor.outcome == "created"
                assert successor.publication_id is not None
                assert successor.schedule_entry_id is not None
                assert successor.legacy_post_task_id is None

                child = await session.get(Publication, int(successor.publication_id))
                child_schedule = await session.get(
                    ScheduleEntry, int(successor.schedule_entry_id)
                )
                assert child is not None and child_schedule is not None
                assert child.execution_mode == CANONICAL_EXECUTION_MODE
                assert child.legacy_post_task_id is None
                assert child.repeat_source_publication_id == root_id
                assert dict(child.meta or {}).get("runtime_options") == {
                    "autodelete_seconds": 600,
                    "autodelete_report": True,
                }
                assert dict(child_schedule.meta or {}).get("runtime_options") == {
                    "autodelete_seconds": 600,
                    "autodelete_report": True,
                }
                schedules, publications, tasks, _revisions = await _counts(session)
                assert (schedules, publications, tasks) == (2, 2, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_historical_linked_repeat_report_remains_linked_legacy() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _owner, channel = await _seed_channel(Session, 7)
            async with Session() as session:
                task = PostTask(
                    channel_id=int(channel.id),
                    status="pending",
                    scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                    payload={
                        "type": "text",
                        "text": "Historical linked repeat report",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "autodelete_seconds": 600,
                        "autodelete_report": True,
                    },
                )
                session.add(task)
                await session.flush()
                publication = await mirror_legacy_post_task(session, task, commit=False)
                assert publication is not None
                await session.commit()
                await session.refresh(publication)

                assert publication.execution_mode == INTENTIONAL_LEGACY_EXECUTION_MODE
                assert publication.legacy_post_task_id == int(task.id)
                admission = await CanonicalSchedulerAdmissionService(session).classify(
                    task_id=int(task.id)
                )
                assert admission.kind is CanonicalSchedulerAdmissionKind.LEGACY_INTENTIONAL
                assert admission.legacy_allowed is True
                assert admission.repeat is True

                successors = int(
                    (
                        await session.execute(
                            select(func.count(Publication.id)).where(
                                Publication.repeat_source_publication_id
                                == int(publication.id)
                            )
                        )
                    ).scalar_one()
                )
                assert successors == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
