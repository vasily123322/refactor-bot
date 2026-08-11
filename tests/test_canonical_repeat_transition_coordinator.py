from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import (
    CanonicalRepeatPlanReservationService,
)
from app.services.canonical_repeat_transition_coordinator import (
    CanonicalRepeatTransitionCoordinator,
)
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_published(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    repeat: bool = True,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=127000 + seed,
            username=f"canonical-repeat-transition-{seed}",
            full_name=f"Canonical Repeat Transition {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(127100 + seed),
            title=f"Canonical Repeat Transition {seed}",
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
                        "text": "Canonical repeat transition proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule=(
                {"enabled": True, "seconds": 300}
                if repeat
                else {"enabled": False}
            ),
            runtime_options={"silent": True},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert task is not None and schedule is not None
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [1701]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[1701],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=scheduled_at + timedelta(minutes=1),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_transition_creates_one_transport_free_successor_and_is_idempotent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-transition.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_published(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                coordinator = CanonicalRepeatTransitionCoordinator(session)
                first = await coordinator.transition(
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                assert first.outcome == "created"
                assert first.successor_publication_id is not None
                assert first.successor_schedule_entry_id is not None
                assert first.successor_legacy_post_task_id is None
                assert (await session.execute(select(PostTask.id))).scalars().all() == []

                second = await coordinator.transition(
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                assert second.outcome == "existing"
                assert second.successor_publication_id == first.successor_publication_id
                assert second.successor_schedule_entry_id == first.successor_schedule_entry_id
                successor_ids = (
                    await session.execute(
                        select(Publication.id).where(Publication.id != source_id)
                    )
                ).scalars().all()
                assert successor_ids == [first.successor_publication_id]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_transition_materializes_an_existing_pending_reservation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-transition-pending.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_published(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                reserved = await CanonicalRepeatPlanReservationService(
                    session
                ).reserve_next(
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                assert reserved.outcome == "reserved"
                result = await CanonicalRepeatTransitionCoordinator(session).transition(
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                assert result.outcome == "created"
                assert result.successor_legacy_post_task_id is None
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_transition_reports_existing_legacy_transport_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-transition-transport.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_published(
                Session,
                seed=3,
                scheduled_at=source_at,
            )

            async with Session() as session:
                reserved = await CanonicalRepeatPlanReservationService(
                    session
                ).reserve_next(
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                assert reserved.outcome == "reserved"
                legacy = await CanonicalRepeatTransportAdapter(session).materialize(source_id)
                assert legacy.outcome == "created"
                result = await CanonicalRepeatTransitionCoordinator(session).transition(
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                assert result.outcome == "existing_transport"
                assert result.successor_publication_id == legacy.publication_id
                assert result.successor_schedule_entry_id == legacy.schedule_entry_id
                assert result.successor_legacy_post_task_id == legacy.legacy_post_task_id
                successors = (
                    await session.execute(
                        select(Publication.id).where(Publication.id != source_id)
                    )
                ).scalars().all()
                assert successors == [legacy.publication_id]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_nonrepeat_source_is_ineligible_without_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-transition-ineligible.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id = await _seed_published(
                Session,
                seed=4,
                scheduled_at=source_at,
                repeat=False,
            )

            async with Session() as session:
                result = await CanonicalRepeatTransitionCoordinator(session).transition(
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                assert result.outcome == "ineligible"
                successors = (
                    await session.execute(
                        select(Publication.id).where(Publication.id != source_id)
                    )
                ).scalars().all()
                assert successors == []
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())
