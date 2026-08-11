from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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
from app.services.canonical_repeat_recovery_verifier import (
    CanonicalRepeatRecoveryVerifier,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


async def _seed_queued_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=105000 + seed,
            username=f"repeat-recovery-verify-{seed}",
            full_name=f"Repeat Recovery Verify {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(105100 + seed),
            title=f"Repeat Recovery Verify {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Recovery verify"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
                "nested": {"mode": "stable"},
            },
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


async def _legacy_recover(
    session,
    *,
    task_id: int,
    after: datetime,
) -> None:
    task = await session.get(PostTask, task_id)
    assert task is not None
    scheduler = PublicationScheduler(session, object())
    scheduler._boot_time = after  # noqa: SLF001 - exact legacy recovery boundary
    skipped = await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
        session,
        task,
        dict(task.payload or {}),
    )
    assert skipped is True
    # The real PublicationScheduler runtime projects the terminal source after the
    # base scheduler returns. Exercise that post-processing boundary too so the
    # verifier sees the same canonical skipped audit as production.
    await scheduler._project_publication(session, task)  # noqa: SLF001


def test_verifier_tracks_real_legacy_recovery_and_survives_source_transport_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-verifier.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_id=publication_id, after=after)
                verifier = CanonicalRepeatRecoveryVerifier(session)
                pending = await verifier.verify(publication_id)
                assert pending.outcome == "pending"

                await _legacy_recover(session, task_id=task_id, after=after)
                matched = await verifier.verify(publication_id)
                assert matched.outcome == "matched"
                assert matched.successor_publication_id is not None
                assert matched.successor_schedule_entry_id is not None
                assert matched.successor_legacy_post_task_id is not None

                source = await session.get(Publication, publication_id)
                assert source is not None
                source_schedule = await session.get(
                    ScheduleEntry,
                    int(source.schedule_entry_id or 0),
                )
                attempts = list(
                    (
                        await session.execute(
                            PublicationAttempt.__table__.select()
                            .where(PublicationAttempt.publication_id == publication_id)
                            .order_by(PublicationAttempt.attempt.asc())
                        )
                    ).mappings().all()
                )
                assert source.status == "skipped"
                assert source_schedule is not None and source_schedule.status == "skipped"
                assert source.attempt_count == 1
                assert len(attempts) == 1
                assert attempts[0]["attempt"] == 1
                assert attempts[0]["status"] == "skipped"
                assert attempts[0]["finished_at"] is not None

                source_task = await session.get(PostTask, task_id)
                assert source_task is not None
                source.legacy_post_task_id = None
                await session.delete(source_task)
                await session.commit()

                after_retirement = await verifier.verify(publication_id)
                assert after_retirement.outcome == "matched"
                assert (
                    after_retirement.successor_publication_id
                    == matched.successor_publication_id
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_verifier_fails_closed_when_recovered_child_runtime_intent_drifts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-verifier-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_id=publication_id, after=after)
                await _legacy_recover(session, task_id=task_id, after=after)
                verifier = CanonicalRepeatRecoveryVerifier(session)
                matched = await verifier.verify(publication_id)
                assert matched.outcome == "matched"
                assert matched.successor_schedule_entry_id is not None

                child_schedule = await session.get(
                    ScheduleEntry,
                    int(matched.successor_schedule_entry_id),
                )
                assert child_schedule is not None
                child_schedule.meta = {
                    **dict(child_schedule.meta or {}),
                    "runtime_options": {"autodelete_views": 999},
                }
                await session.commit()

                conflict = await verifier.verify(publication_id)
                assert conflict.outcome == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())
