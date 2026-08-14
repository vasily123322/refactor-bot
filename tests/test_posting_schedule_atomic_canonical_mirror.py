from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.posting import PostingService
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


async def _publication_for(Session, post_task_id: int) -> Publication | None:
    async with Session() as session:
        return (
            await session.execute(
                select(Publication).where(
                    Publication.legacy_post_task_id == int(post_task_id)
                )
            )
        ).scalar_one_or_none()


def _same_instant(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


def test_supported_deferred_schedule_commits_with_canonical_publication_and_schedule() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 1)
            when = datetime.now(timezone.utc) + timedelta(hours=2)
            task = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {"type": "text", "text": "Deferred atomic mirror"},
                when,
                dedupe_key="atomic-deferred",
            )

            async with Session() as session:
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(task.id)
                        )
                    )
                ).scalar_one()
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id)
                )
                assert schedule is not None
                assert publication.status == "queued"
                assert schedule.status == "pending"
                assert publication.channel_id == task.channel_id == schedule.channel_id
                assert _same_instant(schedule.scheduled_at, when)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_supported_pro_repeat_schedule_is_linked_in_same_commit() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 2)
            task = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {
                    "type": "text",
                    "text": "Pro repeat",
                    "repeat_on": True,
                    "repeat_seconds": 3600,
                },
                datetime.now(timezone.utc) + timedelta(hours=1),
                dedupe_key="atomic-pro-repeat",
            )
            async with Session() as session:
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(task.id)
                        )
                    )
                ).scalar_one()
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id)
                )
                assert schedule is not None
                assert dict(schedule.repeat_rule or {}) == {
                    "enabled": True,
                    "seconds": 3600,
                }
                assert dict(publication.meta or {}).get("repeat_group_id") == task.id
                assert dict(schedule.meta or {}).get("repeat_group_id") == task.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_supported_pending_task_is_never_committed_unlinked_for_scheduler() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 3)
            task = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {"type": "text", "text": "Scheduler visibility"},
                datetime.now(timezone.utc) + timedelta(minutes=30),
                dedupe_key="atomic-visible",
            )
            async with Session() as session:
                pending = list(
                    (
                        await session.execute(
                            select(PostTask).where(PostTask.status == "pending")
                        )
                    ).scalars().all()
                )
                linked_ids = set(
                    (
                        await session.execute(
                            select(Publication.legacy_post_task_id).where(
                                Publication.legacy_post_task_id.is_not(None)
                            )
                        )
                    ).scalars().all()
                )
                assert int(task.id) in {int(row.id) for row in pending}
                assert all(int(row.id) in linked_ids for row in pending)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_materialization_failure_rolls_back_executable_post_task() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        original = posting_module.mirror_legacy_post_task

        async def _fail(*args, **kwargs):
            raise RuntimeError("fixture canonical materialization failure")

        try:
            channel = await _channel(Session, 4)
            posting_module.mirror_legacy_post_task = _fail
            try:
                await PostingService(_Bot(), Session).schedule(
                    int(channel.id),
                    {"type": "text", "text": "Must roll back"},
                    datetime.now(timezone.utc) + timedelta(minutes=15),
                    dedupe_key="atomic-materialize-failure",
                )
                raise AssertionError("schedule unexpectedly committed")
            except RuntimeError as exc:
                assert "materialization failure" in str(exc)
            finally:
                posting_module.mirror_legacy_post_task = original

            async with Session() as session:
                task = (
                    await session.execute(
                        select(PostTask).where(
                            PostTask.dedupe_key == "atomic-materialize-failure"
                        )
                    )
                ).scalar_one_or_none()
                assert task is None
        finally:
            posting_module.mirror_legacy_post_task = original
            await engine.dispose()

    asyncio.run(run())


def test_unsupported_payload_remains_intentional_legacy_fallback() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 5)
            task = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {"type": "unsupported_atomic_fixture", "text": "Legacy fallback"},
                datetime.now(timezone.utc) + timedelta(minutes=20),
                dedupe_key="atomic-unsupported",
            )
            async with Session() as session:
                persisted = await session.get(PostTask, int(task.id))
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(task.id)
                        )
                    )
                ).scalar_one_or_none()
                assert persisted is not None
                assert persisted.status == "pending"
                assert publication is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_atomic_mirror_preserves_exact_occurrence_identity_and_content_parity() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 6)
            when = datetime.now(timezone.utc) + timedelta(minutes=45)
            author_id = 7_771_006
            task = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {
                    "type": "text",
                    "text": "Exact identity and parity",
                    "meta": {"author_user_id": author_id},
                },
                when,
                dedupe_key="atomic-parity",
            )
            async with Session() as session:
                persisted = await session.get(PostTask, int(task.id))
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(task.id)
                        )
                    )
                ).scalar_one()
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id)
                )
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

                assert persisted is not None and schedule is not None
                assert publication.legacy_post_task_id == persisted.id
                assert publication.channel_id == schedule.channel_id == channel.id
                assert publication.content_item_id == schedule.content_item_id
                assert publication.content_revision == schedule.content_revision
                assert revision.created_by_tg_user_id == author_id
                assert "Exact identity and parity" in str(revision.document)
                assert _same_instant(schedule.scheduled_at, when)
                for marker in (
                    "_publication_id",
                    "_content_item_id",
                    "_content_revision",
                    "_content_channel_id",
                ):
                    assert marker not in dict(persisted.payload or {})
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeated_materialization_race_does_not_duplicate_publication_or_schedule() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 7)
            task = await PostingService(_Bot(), Session).schedule(
                int(channel.id),
                {"type": "text", "text": "Idempotent occurrence"},
                datetime.now(timezone.utc) + timedelta(minutes=50),
                dedupe_key="atomic-race",
            )
            first = await _publication_for(Session, int(task.id))
            assert first is not None
            first_publication_id = int(first.id)
            first_schedule_id = int(first.schedule_entry_id)

            async with Session() as left, Session() as right:
                left_task = await left.get(PostTask, int(task.id))
                right_task = await right.get(PostTask, int(task.id))
                assert left_task is not None and right_task is not None
                left_result, right_result = await asyncio.gather(
                    mirror_legacy_post_task(left, left_task),
                    mirror_legacy_post_task(right, right_task),
                )
                assert int(left_result.id) == first_publication_id
                assert int(right_result.id) == first_publication_id

            async with Session() as session:
                publications = list(
                    (
                        await session.execute(
                            select(Publication).where(
                                Publication.legacy_post_task_id == int(task.id)
                            )
                        )
                    ).scalars().all()
                )
                schedules = list((await session.execute(select(ScheduleEntry))).scalars())
                occurrence_schedules = [
                    row
                    for row in schedules
                    if dict(row.meta or {}).get("legacy_post_task_id") == int(task.id)
                ]
                assert [int(row.id) for row in publications] == [first_publication_id]
                assert [int(row.id) for row in occurrence_schedules] == [first_schedule_id]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_repeat_transport_remains_b_seam_and_reuses_root_content() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 8)
            service = PostingService(_Bot(), Session)
            root = await service.schedule(
                int(channel.id),
                {
                    "type": "text",
                    "text": "Canonical repeat root",
                    "repeat_on": True,
                    "repeat_seconds": 1800,
                    "silent": True,
                },
                datetime.now(timezone.utc) + timedelta(minutes=10),
                dedupe_key="atomic-repeat-root",
            )

            async with Session() as session:
                root_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(root.id)
                        )
                    )
                ).scalar_one()
                root_schedule = await session.get(
                    ScheduleEntry, int(root_publication.schedule_entry_id)
                )
                assert root_schedule is not None
                root_content = (
                    int(root_publication.content_item_id),
                    int(root_publication.content_revision),
                )
                publication_meta = dict(root_publication.meta or {})
                schedule_meta = dict(root_schedule.meta or {})
                publication_meta["runtime_options"] = {"silent": True}
                schedule_meta["runtime_options"] = {"silent": True}
                root_publication.meta = publication_meta
                root_schedule.meta = schedule_meta
                await session.commit()

            child = await service.schedule(
                int(channel.id),
                {
                    "type": "text",
                    "text": "Repeat transport child",
                    "repeat_on": True,
                    "repeat_seconds": 1800,
                    "repeat_group_id": int(root.id),
                    "silent": True,
                },
                datetime.now(timezone.utc) + timedelta(minutes=40),
                dedupe_key="atomic-repeat-child",
            )

            async with Session() as session:
                child_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(child.id)
                        )
                    )
                ).scalar_one()
                child_schedule = await session.get(
                    ScheduleEntry, int(child_publication.schedule_entry_id)
                )
                assert child_schedule is not None
                assert int(child_publication.id) != int(root_publication.id)
                assert (
                    int(child_publication.content_item_id),
                    int(child_publication.content_revision),
                ) == root_content
                assert dict(child_publication.meta or {}).get("runtime_options") == {
                    "silent": True
                }
                assert dict(child_schedule.meta or {}).get("runtime_options") == {
                    "silent": True
                }
                assert dict(child_publication.meta or {}).get(
                    "repeat_runtime_intent_provenance"
                ) is True
                assert dict(child_schedule.meta or {}).get(
                    "repeat_runtime_intent_provenance"
                ) is True
                assert child_publication.legacy_post_task_id == child.id
        finally:
            await engine.dispose()

    asyncio.run(run())
