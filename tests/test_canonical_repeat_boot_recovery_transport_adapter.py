from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_boot_recovery_reservation import (
    CanonicalRepeatBootRecoveryReservationService,
)
from app.services.canonical_repeat_boot_recovery_transport_adapter import (
    CanonicalRepeatBootRecoveryTransportAdapter,
)
from app.services.canonical_repeat_boot_recovery_verifier import (
    CanonicalRepeatBootRecoveryVerifier,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import cleanup_runtime_fields


_RUNTIME_OPTIONS = {
    "autodelete_seconds": 3600,
    "autodelete_report": True,
    "nested": {"mode": "stable"},
}


async def _seed_group(
    Session,
    *,
    seed: int,
    source_at: datetime,
) -> tuple[tuple[int, int], tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=113000 + seed,
            username=f"repeat-boot-adapter-{seed}",
            full_name=f"Repeat Boot Adapter {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(113100 + seed),
            title=f"Repeat Boot Adapter {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Boot adapter"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        root_publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=source_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        root_task_id = int(root_publication.legacy_post_task_id or 0)
        root_task = await session.get(PostTask, root_task_id)
        assert root_task is not None

        second_payload = cleanup_runtime_fields(dict(root_task.payload or {}))
        second_payload["repeat_on"] = True
        second_payload["repeat_seconds"] = 3600
        second_payload["repeat_group_id"] = root_task_id
        second_task = PostTask(
            channel_id=int(root_task.channel_id),
            status="pending",
            payload=second_payload,
            dedupe_key=None,
            scheduled_at=source_at + timedelta(hours=1),
        )
        session.add(second_task)
        await session.commit()
        await session.refresh(second_task)
        second_publication = await mirror_legacy_post_task(session, second_task)
        assert second_publication is not None
        return (
            (int(root_publication.id), int(second_publication.id)),
            (root_task_id, int(second_task.id)),
        )


async def _reserve(
    session,
    *,
    publication_ids: tuple[int, int],
    after: datetime,
) -> None:
    result = await CanonicalRepeatBootRecoveryReservationService(session).reserve_group(
        publication_ids,
        after=after,
    )
    assert result.outcome == "reserved"


async def _counts(session) -> tuple[int, int, int, int]:
    publications = int((await session.execute(select(func.count(Publication.id)))).scalar_one())
    schedules = int((await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one())
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    attempts = int(
        (await session.execute(select(func.count(PublicationAttempt.id)))).scalar_one()
    )
    return publications, schedules, tasks, attempts


def test_group_adapter_materializes_one_child_after_all_source_transports_retired(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-adapter-retired.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            publication_ids, task_ids = await _seed_group(
                Session,
                seed=1,
                source_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_ids=publication_ids, after=after)
                root = await session.get(Publication, publication_ids[0])
                assert root is not None
                content_item_id = int(root.content_item_id)
                content_revision = int(root.content_revision)
                repeat_group_id = task_ids[0]

                for publication_id, task_id in zip(publication_ids, task_ids, strict=True):
                    publication = await session.get(Publication, publication_id)
                    task = await session.get(PostTask, task_id)
                    assert publication is not None and task is not None
                    publication.legacy_post_task_id = None
                    await session.delete(task)
                await session.commit()
                before = await _counts(session)

                adapter = CanonicalRepeatBootRecoveryTransportAdapter(session)
                created = await adapter.materialize(publication_ids)
                after_created = await _counts(session)
                existing = await adapter.materialize(publication_ids)
                after_existing = await _counts(session)

                assert created.outcome == "created"
                assert created.publication_id is not None
                assert created.schedule_entry_id is not None
                assert created.legacy_post_task_id is not None
                assert after_created == (
                    before[0] + 1,
                    before[1] + 1,
                    before[2] + 1,
                    before[3] + len(publication_ids),
                )
                assert existing.outcome == "existing"
                assert existing.publication_id == created.publication_id
                assert existing.legacy_post_task_id == created.legacy_post_task_id
                assert after_existing == after_created

                for publication_id in publication_ids:
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    schedule = await session.get(
                        ScheduleEntry,
                        int(publication.schedule_entry_id or 0),
                    )
                    attempts = list(
                        (
                            await session.execute(
                                select(PublicationAttempt).where(
                                    PublicationAttempt.publication_id == publication_id
                                )
                            )
                        ).scalars().all()
                    )
                    assert publication.status == "skipped"
                    assert schedule is not None and schedule.status == "skipped"
                    assert publication.attempt_count == 1
                    assert len(attempts) == 1
                    assert attempts[0].status == "skipped"
                    assert attempts[0].finished_at is not None
                    assert attempts[0].telegram_message_ids is None
                    assert attempts[0].error is None

                child = await session.get(Publication, int(created.publication_id))
                child_schedule = await session.get(
                    ScheduleEntry,
                    int(created.schedule_entry_id),
                )
                child_task = await session.get(PostTask, int(created.legacy_post_task_id))
                assert child is not None and child_schedule is not None and child_task is not None
                assert child.content_item_id == content_item_id
                assert child.content_revision == content_revision
                assert child.status == "queued"
                assert child_schedule.status == "pending"
                assert child_schedule.scheduled_at == target_at.replace(
                    tzinfo=None
                ) or child_schedule.scheduled_at == target_at
                assert child.meta["repeat_group_id"] == repeat_group_id
                assert child_schedule.meta["repeat_group_id"] == repeat_group_id
                assert child.meta["runtime_options"] == _RUNTIME_OPTIONS
                assert child_schedule.meta["runtime_options"] == _RUNTIME_OPTIONS
                assert child.meta["canonical_repeat_boot_recovery_transport_adapter"] is True
                assert child.meta[
                    "canonical_repeat_boot_recovery_source_publication_ids"
                ] == list(publication_ids)
                assert child_task.payload["repeat_on"] is True
                assert child_task.payload["repeat_seconds"] == 3600
                assert child_task.payload["repeat_group_id"] == repeat_group_id
                assert child_task.payload["autodelete_seconds"] == 3600
                assert child_task.payload["autodelete_report"] is True
                assert child_task.payload["nested"] == {"mode": "stable"}
                assert child_task.payload["autodelete_at"] == (
                    target_at + timedelta(hours=1)
                ).isoformat()
                assert child_task.dedupe_key == (
                    f"canonical-repeat-boot-recovery:{repeat_group_id}:"
                    f"{target_at.isoformat()}"
                )

                verification = await CanonicalRepeatBootRecoveryVerifier(session).verify(
                    publication_ids
                )
                assert verification.outcome == "matched"
                assert verification.successor_publication_id == int(child.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_group_adapter_marks_all_exact_source_transports_skipped(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-adapter-present.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_ids, task_ids = await _seed_group(
                Session,
                seed=2,
                source_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_ids=publication_ids, after=after)
                result = await CanonicalRepeatBootRecoveryTransportAdapter(
                    session
                ).materialize(publication_ids)
                assert result.outcome == "created"

                for task_id in task_ids:
                    task = await session.get(PostTask, task_id)
                    assert task is not None
                    assert task.status == "skipped"
                    assert task.error == "overdue at boot"

                verification = await CanonicalRepeatBootRecoveryVerifier(session).verify(
                    publication_ids
                )
                assert verification.outcome == "matched"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_group_adapter_refuses_exact_unmirrored_target_transport_without_source_writes(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-adapter-target.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            publication_ids, task_ids = await _seed_group(
                Session,
                seed=3,
                source_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_ids=publication_ids, after=after)
                root = await session.get(Publication, publication_ids[0])
                assert root is not None
                existing_transport = PostTask(
                    channel_id=int(root.channel_id),
                    status="pending",
                    scheduled_at=target_at,
                    payload={
                        "type": "text",
                        "text": "Existing group target transport",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "repeat_group_id": task_ids[0],
                    },
                )
                session.add(existing_transport)
                await session.commit()
                await session.refresh(existing_transport)
                existing_transport_id = int(existing_transport.id)
                before = await _counts(session)

                result = await CanonicalRepeatBootRecoveryTransportAdapter(
                    session
                ).materialize(publication_ids)
                after_counts = await _counts(session)

                assert result.outcome == "existing_transport"
                assert result.legacy_post_task_id == existing_transport_id
                assert before == after_counts
                for publication_id in publication_ids:
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    schedule = await session.get(
                        ScheduleEntry,
                        int(publication.schedule_entry_id or 0),
                    )
                    assert publication.status == "queued"
                    assert schedule is not None and schedule.status == "pending"
                    assert publication.attempt_count == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_group_adapter_fails_closed_on_post_reservation_source_drift(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-adapter-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_ids, _task_ids = await _seed_group(
                Session,
                seed=4,
                source_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_ids=publication_ids, after=after)
                second = await session.get(Publication, publication_ids[1])
                assert second is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(second.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"autodelete_seconds": 999},
                }
                await session.commit()
                before = await _counts(session)

                result = await CanonicalRepeatBootRecoveryTransportAdapter(
                    session
                ).materialize(publication_ids)
                after_counts = await _counts(session)

                assert result.outcome == "conflict"
                assert before == after_counts
        finally:
            await engine.dispose()

    asyncio.run(run())
