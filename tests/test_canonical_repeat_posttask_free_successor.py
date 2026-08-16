from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.canonical_repeat_transport_adapter import (
    CanonicalRepeatTransportAdapter,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_execution_mode import CANONICAL_EXECUTION_MODE


_RUNTIME_OPTIONS = {
    "pin_on": True,
    "forward_to": [123456789],
    "autodelete_seconds": 3600,
}


async def _session_factory(tmp_path, name: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_canonical_source(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=109000 + seed,
            username=f"repeat-posttask-free-{seed}",
            full_name=f"Repeat PostTask Free {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(109100 + seed),
            title=f"Repeat PostTask Free {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Canonical repeat {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=_RUNTIME_OPTIONS,
        )
        assert publication.execution_mode == CANONICAL_EXECUTION_MODE
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id or 0))
        assert task is not None and schedule is not None

        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [109200 + seed]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[109200 + seed],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=scheduled_at,
            )
        )
        publication.legacy_post_task_id = None
        await session.flush()
        await session.delete(task)
        await session.commit()
        return int(publication.id), task_id


async def _reserve(session, publication_id: int, *, after: datetime):
    result = await CanonicalRepeatPlanReservationService(session).reserve_next(
        publication_id,
        after=after,
    )
    assert result.outcome == "reserved"
    assert result.plan is not None
    return result.plan


async def _counts(session) -> tuple[int, int, int]:
    publications = int(
        (await session.execute(select(func.count(Publication.id)))).scalar_one()
    )
    schedules = int(
        (await session.execute(select(func.count(ScheduleEntry.id)))).scalar_one()
    )
    tasks = int((await session.execute(select(func.count(PostTask.id)))).scalar_one())
    return publications, schedules, tasks


def test_supported_canonical_repeat_successor_is_fully_posttask_free(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "repeat-posttask-free.db")
        try:
            source_at = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            publication_id, root_task_id = await _seed_canonical_source(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                plan = await _reserve(
                    session,
                    publication_id,
                    after=source_at,
                )
                before = await _counts(session)

                created = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                after_created = await _counts(session)
                existing = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                after_existing = await _counts(session)

                assert created.outcome == "created"
                assert created.publication_id is not None
                assert created.schedule_entry_id is not None
                assert created.legacy_post_task_id is None
                assert after_created == (before[0] + 1, before[1] + 1, before[2])
                assert existing.outcome == "existing"
                assert existing.publication_id == created.publication_id
                assert existing.legacy_post_task_id is None
                assert after_existing == after_created

                child = await session.get(Publication, int(created.publication_id))
                child_schedule = await session.get(
                    ScheduleEntry,
                    int(created.schedule_entry_id),
                )
                assert child is not None and child_schedule is not None
                assert child.execution_mode == CANONICAL_EXECUTION_MODE
                assert child.repeat_source_publication_id == publication_id
                assert child.legacy_post_task_id is None
                assert child.status == "queued"
                assert child.attempt_count == 0
                assert child.telegram_message_ids is None
                assert child.result_link is None
                assert child.last_error is None
                assert child.meta["repeat_group_id"] == root_task_id
                assert child.meta["canonical_repeat_source_publication_id"] == publication_id
                assert child.meta["canonical_repeat_posttask_free"] is True
                assert child.meta["runtime_options"] == _RUNTIME_OPTIONS
                assert child_schedule.meta["runtime_options"] == _RUNTIME_OPTIONS
                assert child_schedule.repeat_rule == {"enabled": True, "seconds": 3600}
                assert child_schedule.scheduled_at == plan.scheduled_at.replace(
                    tzinfo=None
                ) or child_schedule.scheduled_at == plan.scheduled_at

                child_attempts = int(
                    (
                        await session.execute(
                            select(func.count(PublicationAttempt.id)).where(
                                PublicationAttempt.publication_id == int(child.id)
                            )
                        )
                    ).scalar_one()
                )
                child_leases = int(
                    (
                        await session.execute(
                            select(func.count(PublicationDeliveryLease.id)).where(
                                PublicationDeliveryLease.publication_id == int(child.id)
                            )
                        )
                    ).scalar_one()
                )
                assert child_attempts == 0
                assert child_leases == 0

                verification = await CanonicalRepeatReservationVerifier(session).verify(
                    publication_id
                )
                assert verification.outcome == "matched"
                assert verification.successor_publication_id == int(child.id)
                assert verification.successor_legacy_post_task_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_exact_legacy_transport_blocks_canonical_successor(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "repeat-existing-transport.db")
        try:
            source_at = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            publication_id, root_task_id = await _seed_canonical_source(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                plan = await _reserve(session, publication_id, after=source_at)
                source = await session.get(Publication, publication_id)
                assert source is not None
                transport = PostTask(
                    channel_id=int(source.channel_id),
                    status="pending",
                    scheduled_at=plan.scheduled_at,
                    payload={
                        "type": "text",
                        "text": "existing legacy owner",
                        "repeat_on": True,
                        "repeat_seconds": 3600,
                        "repeat_group_id": root_task_id,
                    },
                )
                session.add(transport)
                await session.commit()
                await session.refresh(transport)
                before = await _counts(session)

                result = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                after = await _counts(session)

                assert result.outcome == "existing_transport"
                assert result.legacy_post_task_id == int(transport.id)
                assert after == before
                successor = (
                    await session.execute(
                        select(Publication).where(
                            Publication.repeat_source_publication_id == publication_id
                        )
                    )
                ).scalar_one_or_none()
                assert successor is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_source_unique_constraint_is_exactly_one_successor_guard(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _session_factory(tmp_path, "repeat-source-unique.db")
        try:
            source_at = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            publication_id, _ = await _seed_canonical_source(
                Session,
                seed=3,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(session, publication_id, after=source_at)
                created = await CanonicalRepeatTransportAdapter(session).materialize(
                    publication_id
                )
                assert created.outcome == "created"
                source = await session.get(Publication, publication_id)
                assert source is not None
                duplicate_schedule = ScheduleEntry(
                    content_item_id=int(source.content_item_id),
                    content_revision=int(source.content_revision),
                    channel_id=int(source.channel_id),
                    scheduled_at=source_at.replace(hour=18),
                    timezone=None,
                    status="pending",
                    repeat_rule={"enabled": True, "seconds": 3600},
                    meta={"repeat_group_id": 1},
                )
                session.add(duplicate_schedule)
                await session.flush()
                duplicate = Publication(
                    schedule_entry_id=int(duplicate_schedule.id),
                    content_item_id=int(source.content_item_id),
                    content_revision=int(source.content_revision),
                    channel_id=int(source.channel_id),
                    status="queued",
                    execution_mode=CANONICAL_EXECUTION_MODE,
                    repeat_source_publication_id=publication_id,
                    meta={},
                )
                session.add(duplicate)
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()

                successors = int(
                    (
                        await session.execute(
                            select(func.count(Publication.id)).where(
                                Publication.repeat_source_publication_id == publication_id
                            )
                        )
                    ).scalar_one()
                )
                assert successors == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
