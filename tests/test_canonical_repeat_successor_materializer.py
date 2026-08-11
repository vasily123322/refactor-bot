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
from app.services.canonical_repeat_successor_materializer import (
    CanonicalRepeatSuccessorMaterializer,
)
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter
from app.services.publication_bridge import LegacyPublicationBridge


_RUNTIME_OPTIONS = {"silent": True, "autodelete_seconds": 120}


async def _seed_published_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    repeat_seconds: int = 300,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=125000 + seed,
            username=f"canonical-repeat-successor-{seed}",
            full_name=f"Canonical Repeat Successor {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(125100 + seed),
            title=f"Canonical Repeat Successor {seed}",
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
                        "text": "Canonical repeat successor proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": repeat_seconds},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert task is not None and schedule is not None
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [1501]
        publication.result_link = "https://t.me/c/12345/1501"
        publication.last_error = None
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[1501],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=scheduled_at + timedelta(minutes=1),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id), int(channel.id)


async def _reserve(
    session,
    publication_id: int,
    *,
    after: datetime,
):
    reservation = await CanonicalRepeatPlanReservationService(session).reserve_next(
        publication_id,
        after=after,
    )
    assert reservation.outcome in {"reserved", "already_reserved"}
    assert reservation.plan is not None
    return reservation.plan


def test_materialize_creates_canonical_successor_without_any_post_task(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-successor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id, channel_id = await _seed_published_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                plan = await _reserve(
                    session,
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                result = await CanonicalRepeatSuccessorMaterializer(session).materialize(
                    source_id
                )
                assert result.outcome == "created"
                assert result.publication_id is not None
                assert result.schedule_entry_id is not None
                assert result.legacy_post_task_id is None

                successor = await session.get(Publication, result.publication_id)
                successor_schedule = await session.get(
                    ScheduleEntry,
                    result.schedule_entry_id,
                )
                assert successor is not None and successor_schedule is not None
                assert successor.status == "queued"
                assert successor.legacy_post_task_id is None
                assert successor.channel_id == channel_id
                assert successor.content_item_id == plan.content_item_id
                assert successor.content_revision == plan.content_revision
                assert successor.meta["repeat_group_id"] == plan.repeat_group_id
                assert successor.meta["canonical_repeat_source_publication_id"] == source_id
                assert successor.meta["canonical_repeat_successor_materializer"] is True
                assert successor.meta["runtime_options"] == _RUNTIME_OPTIONS
                assert successor_schedule.status == "pending"
                assert successor_schedule.scheduled_at == plan.scheduled_at.replace(tzinfo=None)
                assert successor_schedule.repeat_rule == {
                    "enabled": True,
                    "seconds": plan.repeat_seconds,
                }

                task_ids = (await session.execute(select(PostTask.id))).scalars().all()
                assert task_ids == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_materialize_is_idempotent_for_exact_canonical_only_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-successor-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id, _channel_id = await _seed_published_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(
                    session,
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                materializer = CanonicalRepeatSuccessorMaterializer(session)
                first = await materializer.materialize(source_id)
                second = await materializer.materialize(source_id)
                assert first.outcome == "created"
                assert second.outcome == "existing"
                assert second.publication_id == first.publication_id
                assert second.schedule_entry_id == first.schedule_entry_id
                publications = (
                    await session.execute(
                        select(Publication.id).where(Publication.id != source_id)
                    )
                ).scalars().all()
                assert publications == [first.publication_id]
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_legacy_transport_successor_is_detected_without_duplicate(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-existing-transport.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id, _channel_id = await _seed_published_repeat(
                Session,
                seed=3,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(
                    session,
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                legacy = await CanonicalRepeatTransportAdapter(session).materialize(source_id)
                assert legacy.outcome == "created"
                assert legacy.legacy_post_task_id is not None

                result = await CanonicalRepeatSuccessorMaterializer(session).materialize(
                    source_id
                )
                assert result.outcome == "existing_transport"
                assert result.publication_id == legacy.publication_id
                assert result.schedule_entry_id == legacy.schedule_entry_id
                assert result.legacy_post_task_id == legacy.legacy_post_task_id
                successors = (
                    await session.execute(
                        select(Publication.id).where(Publication.id != source_id)
                    )
                ).scalars().all()
                assert successors == [legacy.publication_id]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_canonical_successor_metadata_drift_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-successor-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            source_id, _channel_id = await _seed_published_repeat(
                Session,
                seed=4,
                scheduled_at=source_at,
            )

            async with Session() as session:
                await _reserve(
                    session,
                    source_id,
                    after=source_at + timedelta(minutes=1),
                )
                materializer = CanonicalRepeatSuccessorMaterializer(session)
                first = await materializer.materialize(source_id)
                assert first.outcome == "created"
                successor = await session.get(Publication, int(first.publication_id or 0))
                assert successor is not None
                successor.meta = {
                    **dict(successor.meta or {}),
                    "runtime_options": {"silent": False},
                }
                await session.commit()

                conflict = await materializer.materialize(source_id)
                assert conflict.outcome == "conflict"
                successors = (
                    await session.execute(
                        select(Publication.id).where(Publication.id != source_id)
                    )
                ).scalars().all()
                assert successors == [first.publication_id]
        finally:
            await engine.dispose()

    asyncio.run(run())
