from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_recovery_reservation import (
    CanonicalRepeatRecoveryReservationService,
)
from app.services.canonical_repeat_recovery_transport_adapter import (
    CanonicalRepeatRecoveryTransportAdapter,
)
from app.services.canonical_repeat_recovery_verifier import (
    CanonicalRepeatRecoveryVerifier,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_queued_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    mode: str = "time",
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=106000 + seed,
            username=f"repeat-recovery-adapter-{seed}",
            full_name=f"Repeat Recovery Adapter {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(106100 + seed),
            title=f"Repeat Recovery Adapter {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Recovery adapter"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        runtime_options = (
            {"autodelete_seconds": 3600, "autodelete_report": True}
            if mode == "time"
            else {"autodelete_views": 100, "autodelete_report": True}
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=runtime_options,
        )
        return int(publication.id), int(publication.legacy_post_task_id or 0)


async def _reserve(
    session,
    *,
    publication_id: int,
    after: datetime,
) -> None:
    result = await CanonicalRepeatRecoveryReservationService(session).reserve_recovery(
        publication_id,
        after=after,
    )
    assert result.outcome == "reserved"


async def _counts(session) -> tuple[int, int, int]:
    publications = int((await session.execute(select(func.count(Publication.id)))).scalar_one())
    schedules = int((await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one())
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    return publications, schedules, tasks


def test_recovery_adapter_atomically_skips_source_and_creates_child_without_source_transport(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-adapter-no-source.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            publication_id, source_task_id = await _seed_queued_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
                mode="time",
            )

            async with Session() as session:
                await _reserve(session, publication_id=publication_id, after=after)
                source = await session.get(Publication, publication_id)
                source_task = await session.get(PostTask, source_task_id)
                assert source is not None and source_task is not None
                source_content_item_id = int(source.content_item_id)
                source_content_revision = int(source.content_revision)
                source.legacy_post_task_id = None
                await session.delete(source_task)
                await session.commit()
                before = await _counts(session)

                adapter = CanonicalRepeatRecoveryTransportAdapter(session)
                created = await adapter.materialize(publication_id)
                after_created = await _counts(session)
                existing = await adapter.materialize(publication_id)
                after_existing = await _counts(session)

                assert created.outcome == "created"
                assert created.publication_id is not None
                assert created.schedule_entry_id is not None
                assert created.legacy_post_task_id is not None
                assert after_created == (
                    before[0] + 1,
                    before[1] + 1,
                    before[2] + 1,
                )
                assert existing.outcome == "existing"
                assert existing.publication_id == created.publication_id
                assert existing.legacy_post_task_id == created.legacy_post_task_id
                assert after_existing == after_created

                source = await session.get(Publication, publication_id)
                assert source is not None
                source_schedule = await session.get(
                    ScheduleEntry,
                    int(source.schedule_entry_id or 0),
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
                assert source.status == "skipped"
                assert source_schedule is not None and source_schedule.status == "skipped"
                assert source.attempt_count == 1
                assert len(attempts) == 1
                assert attempts[0].attempt == 1
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
                assert child.content_item_id == source_content_item_id
                assert child.content_revision == source_content_revision
                assert child.status == "queued"
                assert child_schedule.status == "pending"
                assert child_schedule.scheduled_at == target_at.replace(
                    tzinfo=None
                ) or child_schedule.scheduled_at == target_at
                assert child.meta["repeat_group_id"] == source_task_id
                assert child_schedule.meta["repeat_group_id"] == source_task_id
                assert child.meta["canonical_repeat_recovery_transport_adapter"] is True
                assert child_schedule.meta[
                    "canonical_repeat_recovery_transport_adapter"
                ] is True
                assert child_task.payload["repeat_on"] is True
                assert child_task.payload["repeat_seconds"] == 3600
                assert child_task.payload["repeat_group_id"] == source_task_id
                assert child_task.payload["autodelete_seconds"] == 3600
                assert child_task.payload["autodelete_report"] is True
                assert child_task.payload["autodelete_at"] == (
                    target_at + timedelta(hours=1)
                ).isoformat()
                assert child_task.dedupe_key == (
                    f"canonical-repeat-recovery:{publication_id}:{target_at.isoformat()}"
                )

                verification = await CanonicalRepeatRecoveryVerifier(session).verify(
                    publication_id
                )
                assert verification.outcome == "matched"
                assert verification.successor_publication_id == int(child.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_adapter_marks_exact_source_transport_skipped_when_present(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-adapter-source.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_id, source_task_id = await _seed_queued_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
                mode="views",
            )

            async with Session() as session:
                await _reserve(session, publication_id=publication_id, after=after)
                result = await CanonicalRepeatRecoveryTransportAdapter(session).materialize(
                    publication_id
                )
                assert result.outcome == "created"

                source_task = await session.get(PostTask, source_task_id)
                assert source_task is not None
                assert source_task.status == "skipped"
                assert source_task.error == "overdue at boot"

                verification = await CanonicalRepeatRecoveryVerifier(session).verify(
                    publication_id
                )
                assert verification.outcome == "matched"
                assert verification.successor_legacy_post_task_id == result.legacy_post_task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_adapter_refuses_duplicate_when_exact_unmirrored_transport_exists(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-adapter-duplicate.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            target_at = source_at + timedelta(hours=4)
            publication_id, source_task_id = await _seed_queued_repeat(
                Session,
                seed=3,
                scheduled_at=source_at,
                mode="views",
            )

            async with Session() as session:
                await _reserve(session, publication_id=publication_id, after=after)
                source = await session.get(Publication, publication_id)
                assert source is not None
                existing_transport = PostTask(
                    channel_id=int(source.channel_id),
                    status="pending",
                    scheduled_at=target_at,
                    payload={
                        "type": "text",
                        "text": "Existing recovery transport",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "repeat_group_id": source_task_id,
                    },
                )
                session.add(existing_transport)
                await session.commit()
                await session.refresh(existing_transport)
                existing_transport_id = int(existing_transport.id)
                before = await _counts(session)

                result = await CanonicalRepeatRecoveryTransportAdapter(session).materialize(
                    publication_id
                )
                after_counts = await _counts(session)

                assert result.outcome == "existing_transport"
                assert result.legacy_post_task_id == existing_transport_id
                assert before == after_counts
                source = await session.get(Publication, publication_id)
                assert source is not None
                source_schedule = await session.get(
                    ScheduleEntry,
                    int(source.schedule_entry_id or 0),
                )
                assert source.status == "queued"
                assert source_schedule is not None and source_schedule.status == "pending"
                assert source.attempt_count == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
