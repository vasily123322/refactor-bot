from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidateSelector,
)
from app.services.content_plan_publication_cancellation import (
    ContentPlanPublicationCancellationService,
)
from app.services.posting import PostingService
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    INTENTIONAL_LEGACY_EXECUTION_MODE,
)
import app.services.posting as posting_module


class _Bot:
    pass


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _channel(Session, suffix: int = 1) -> Channel:
    async with Session() as session:
        owner = Client(
            tg_user_id=9_900_000 + suffix,
            username=f"atomic{suffix}",
            full_name="Atomic Mirror Fixture",
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_900_000_000 - suffix,
            title=f"Atomic {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


def _same_instant(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


async def _direct_canonical(
    Session,
    *,
    channel: Channel,
    payload: dict,
    when: datetime,
    dedupe_key: str,
) -> tuple[Publication, ScheduleEntry]:
    result = await PostingService(_Bot(), Session).schedule(
        int(channel.id), payload, when, dedupe_key=dedupe_key
    )
    assert isinstance(result, Publication)

    async with Session() as session:
        publication = await session.get(Publication, int(result.id))
        assert publication is not None
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert schedule is not None
        return publication, schedule


async def _legacy_schedule(
    Session,
    *,
    channel: Channel,
    payload: dict,
    when: datetime,
    dedupe_key: str,
) -> tuple[PostTask, Publication | None]:
    result = await PostingService(_Bot(), Session).schedule(
        int(channel.id), payload, when, dedupe_key=dedupe_key
    )
    assert isinstance(result, PostTask)

    async with Session() as session:
        task = await session.get(PostTask, int(result.id))
        assert task is not None
        publication = (
            await session.execute(
                select(Publication).where(
                    Publication.legacy_post_task_id == int(task.id)
                )
            )
        ).scalar_one_or_none()
        return task, publication


def test_supported_schedule_materializes_direct_canonical_pair_without_posttask() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 1)
            when = datetime.now(timezone.utc) + timedelta(hours=2)
            publication, schedule = await _direct_canonical(
                Session,
                channel=channel,
                payload={"type": "text", "text": "Direct canonical root"},
                when=when,
                dedupe_key="atomic-direct-root",
            )

            async with Session() as session:
                tasks = list((await session.execute(select(PostTask))).scalars())
                publications = list(
                    (await session.execute(select(Publication))).scalars()
                )
                schedules = list((await session.execute(select(ScheduleEntry))).scalars())

            assert tasks == []
            assert [int(row.id) for row in publications] == [int(publication.id)]
            assert [int(row.id) for row in schedules] == [int(schedule.id)]
            assert publication.execution_mode == CANONICAL_EXECUTION_MODE
            assert publication.legacy_post_task_id is None
            assert publication.status == "queued"
            assert schedule.status == "pending"
            assert publication.schedule_entry_id == schedule.id
            assert publication.channel_id == schedule.channel_id == channel.id
            assert publication.content_item_id == schedule.content_item_id
            assert publication.content_revision == schedule.content_revision
            assert _same_instant(schedule.scheduled_at, when)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_direct_canonical_preserves_immutable_queue_time_runtime_intent_and_content() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 2)
            when = datetime.now(timezone.utc) + timedelta(minutes=45)
            author_id = 7_771_002
            publication, schedule = await _direct_canonical(
                Session,
                channel=channel,
                payload={
                    "type": "text",
                    "text": "Immutable direct intent",
                    "silent": True,
                    "pin_on": True,
                    "forward_to": [int(channel.id)],
                    "meta": {"author_user_id": author_id},
                },
                when=when,
                dedupe_key="atomic-direct-intent",
            )

            expected_runtime = {
                "silent": True,
                "pin_on": True,
                "forward_to": [int(channel.id)],
            }
            assert dict(publication.meta or {}).get("runtime_options") == expected_runtime
            assert dict(schedule.meta or {}).get("runtime_options") == expected_runtime
            assert dict(publication.meta or {}).get("canonical_posttask_free") is True
            assert dict(schedule.meta or {}).get("canonical_posttask_free") is True

            async with Session() as session:
                revision = (
                    await session.execute(
                        select(ContentRevision).where(
                            ContentRevision.content_item_id
                            == int(publication.content_item_id),
                            ContentRevision.revision
                            == int(publication.content_revision),
                        )
                    )
                ).scalar_one()
                assert revision.created_by_tg_user_id == author_id
                assert "Immutable direct intent" in str(revision.document)
                assert list((await session.execute(select(PostTask))).scalars()) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_direct_canonical_repeat_root_uses_publication_identity_and_no_posttask() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 3)
            publication, schedule = await _direct_canonical(
                Session,
                channel=channel,
                payload={
                    "type": "text",
                    "text": "Direct repeat root",
                    "repeat_on": True,
                    "repeat_seconds": 3600,
                    "silent": True,
                },
                when=datetime.now(timezone.utc) + timedelta(hours=1),
                dedupe_key="atomic-direct-repeat",
            )
            group_id = int(publication.id)
            assert dict(schedule.repeat_rule or {}) == {
                "enabled": True,
                "seconds": 3600,
            }
            assert dict(publication.meta or {}).get("repeat_group_id") == group_id
            assert dict(schedule.meta or {}).get("repeat_group_id") == group_id
            assert publication.legacy_post_task_id is None
            async with Session() as session:
                assert list((await session.execute(select(PostTask))).scalars()) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_direct_canonical_occurrence_is_visible_to_candidate_path() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 4)
            when = datetime.now(timezone.utc) + timedelta(minutes=20)
            publication, _ = await _direct_canonical(
                Session,
                channel=channel,
                payload={"type": "text", "text": "Candidate visible"},
                when=when,
                dedupe_key="atomic-direct-candidate",
            )
            async with Session() as session:
                due = await CanonicalPublicationDeliveryCandidateSelector(session).due(
                    at=when + timedelta(minutes=1)
                )
                assert int(publication.id) in {row.publication_id for row in due}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_new_direct_root_cancels_through_publication_native_stage6_path() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 5)
            publication, schedule = await _direct_canonical(
                Session,
                channel=channel,
                payload={"type": "text", "text": "Direct root cancellation"},
                when=datetime.now(timezone.utc) + timedelta(minutes=30),
                dedupe_key="atomic-direct-cancel",
            )
            result = await ContentPlanPublicationCancellationService(
                Session
            ).delete_publication(int(publication.id))
            assert result.outcome == "cancelled"

            async with Session() as session:
                persisted_publication = await session.get(
                    Publication, int(publication.id)
                )
                persisted_schedule = await session.get(ScheduleEntry, int(schedule.id))
                assert persisted_publication is not None
                assert persisted_schedule is not None
                assert persisted_publication.status == "cancelled"
                assert persisted_schedule.status == "cancelled"
                assert persisted_publication.legacy_post_task_id is None
                assert list((await session.execute(select(PostTask))).scalars()) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_direct_canonical_sequential_dedupe_reuses_occurrence_without_legacy_authority() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 6)
            service = PostingService(_Bot(), Session)
            when = datetime.now(timezone.utc) + timedelta(minutes=50)
            first = await service.schedule(
                int(channel.id),
                {"type": "text", "text": "Direct dedupe"},
                when,
                dedupe_key="atomic-direct-dedupe",
            )
            second = await service.schedule(
                int(channel.id),
                {"type": "text", "text": "Direct dedupe duplicate"},
                when + timedelta(minutes=1),
                dedupe_key="atomic-direct-dedupe",
            )
            assert isinstance(first, Publication)
            assert isinstance(second, Publication)
            assert int(first.id) == int(second.id)

            async with Session() as session:
                publications = list(
                    (await session.execute(select(Publication))).scalars()
                )
                schedules = list((await session.execute(select(ScheduleEntry))).scalars())
                tasks = list((await session.execute(select(PostTask))).scalars())
                assert len(publications) == 1
                assert len(schedules) == 1
                assert tasks == []
                assert dict(publications[0].meta or {}).get("posting_dedupe_key") == (
                    "atomic-direct-dedupe"
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_direct_materialization_failure_rolls_back_all_canonical_rows() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        original = posting_module.materialize_new_canonical_occurrence

        async def _fail(session, **kwargs):
            await original(session, **kwargs)
            raise RuntimeError("fixture direct canonical failure")

        try:
            channel = await _channel(Session, 7)
            posting_module.materialize_new_canonical_occurrence = _fail
            try:
                await PostingService(_Bot(), Session).schedule(
                    int(channel.id),
                    {"type": "text", "text": "Must roll back direct rows"},
                    datetime.now(timezone.utc) + timedelta(minutes=15),
                    dedupe_key="atomic-direct-failure",
                )
                raise AssertionError("schedule unexpectedly committed")
            except RuntimeError as exc:
                assert "direct canonical failure" in str(exc)
            finally:
                posting_module.materialize_new_canonical_occurrence = original

            async with Session() as session:
                assert list((await session.execute(select(PostTask))).scalars()) == []
                assert list((await session.execute(select(Publication))).scalars()) == []
                assert list((await session.execute(select(ScheduleEntry))).scalars()) == []
                assert list((await session.execute(select(ContentRevision))).scalars()) == []
        finally:
            posting_module.materialize_new_canonical_occurrence = original
            await engine.dispose()

    asyncio.run(run())


def test_time_views_profile_remains_intentional_legacy_posttask_transport() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 8)
            task, publication = await _legacy_schedule(
                Session,
                channel=channel,
                payload={
                    "type": "text",
                    "text": "Mixed legacy profile",
                    "autodelete_seconds": 600,
                    "autodelete_views": 100,
                },
                when=datetime.now(timezone.utc) + timedelta(minutes=20),
                dedupe_key="atomic-time-views-legacy",
            )
            assert task.status == "pending"
            if publication is not None:
                assert publication.execution_mode == INTENTIONAL_LEGACY_EXECUTION_MODE
                assert publication.legacy_post_task_id == task.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_profile_remains_intentional_legacy_posttask_transport() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 9)
            task, publication = await _legacy_schedule(
                Session,
                channel=channel,
                payload={
                    "type": "text",
                    "text": "Report fallback",
                    "autodelete_seconds": 600,
                    "autodelete_report": True,
                },
                when=datetime.now(timezone.utc) + timedelta(minutes=20),
                dedupe_key="atomic-report-legacy",
            )
            assert task.status == "pending"
            if publication is not None:
                assert publication.execution_mode == INTENTIONAL_LEGACY_EXECUTION_MODE
                assert publication.legacy_post_task_id == task.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unsupported_payload_remains_posttask_fallback_and_is_not_canonicalized() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 10)
            task, publication = await _legacy_schedule(
                Session,
                channel=channel,
                payload={"type": "unsupported_atomic_fixture", "text": "Legacy fallback"},
                when=datetime.now(timezone.utc) + timedelta(minutes=20),
                dedupe_key="atomic-unsupported",
            )
            assert task.status == "pending"
            assert publication is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_new_direct_schedule_does_not_mutate_existing_linked_compatibility_row() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 11)
            legacy_task, legacy_publication = await _legacy_schedule(
                Session,
                channel=channel,
                payload={
                    "type": "text",
                    "text": "Historical linked row",
                    "autodelete_seconds": 600,
                    "autodelete_views": 100,
                },
                when=datetime.now(timezone.utc) + timedelta(minutes=10),
                dedupe_key="atomic-existing-linked",
            )
            linked_publication_id = (
                int(legacy_publication.id) if legacy_publication is not None else None
            )

            direct_publication, _ = await _direct_canonical(
                Session,
                channel=channel,
                payload={"type": "text", "text": "New canonical beside historical"},
                when=datetime.now(timezone.utc) + timedelta(minutes=30),
                dedupe_key="atomic-new-beside-linked",
            )
            assert direct_publication.legacy_post_task_id is None

            async with Session() as session:
                persisted_task = await session.get(PostTask, int(legacy_task.id))
                assert persisted_task is not None
                assert persisted_task.status == "pending"
                if linked_publication_id is not None:
                    persisted_link = await session.get(Publication, linked_publication_id)
                    assert persisted_link is not None
                    assert persisted_link.legacy_post_task_id == legacy_task.id
                    assert (
                        persisted_link.execution_mode
                        == INTENTIONAL_LEGACY_EXECUTION_MODE
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())
