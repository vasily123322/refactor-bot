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
from app.services.canonical_repeat_successor_verifier import (
    CanonicalRepeatSuccessorVerifier,
)
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter
from app.services.publication_bridge import LegacyPublicationBridge


_RUNTIME_OPTIONS = {"silent": True, "autodelete_seconds": 120}


async def _seed_reserved_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=126000 + seed,
            username=f"canonical-repeat-verify-{seed}",
            full_name=f"Canonical Repeat Verify {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(126100 + seed),
            title=f"Canonical Repeat Verify {seed}",
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
                        "text": "Canonical repeat verifier proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 300},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
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
        publication.telegram_message_ids = [1601]
        publication.result_link = "https://t.me/c/12345/1601"
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[1601],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=scheduled_at + timedelta(minutes=1),
            )
        )
        await session.delete(task)
        await session.commit()
        reservation = await CanonicalRepeatPlanReservationService(session).reserve_next(
            int(publication.id),
            after=scheduled_at + timedelta(minutes=1),
        )
        assert reservation.outcome == "reserved"
        return int(publication.id)


def test_verifier_transitions_pending_to_matched_canonical_without_transport(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-verify.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_id = await _seed_reserved_repeat(
                Session,
                seed=1,
                scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            )

            async with Session() as session:
                verifier = CanonicalRepeatSuccessorVerifier(session)
                pending = await verifier.verify(source_id)
                assert pending.outcome == "pending"
                assert pending.successor_publication_id is None

                created = await CanonicalRepeatSuccessorMaterializer(session).materialize(
                    source_id
                )
                assert created.outcome == "created"
                matched = await verifier.verify(source_id)
                assert matched.outcome == "matched_canonical"
                assert matched.successor_publication_id == created.publication_id
                assert matched.successor_schedule_entry_id == created.schedule_entry_id
                assert matched.successor_legacy_post_task_id is None
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_verifier_reports_matching_legacy_transport_without_second_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-verify-transport.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_id = await _seed_reserved_repeat(
                Session,
                seed=2,
                scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            )

            async with Session() as session:
                legacy = await CanonicalRepeatTransportAdapter(session).materialize(source_id)
                assert legacy.outcome == "created"
                verified = await CanonicalRepeatSuccessorVerifier(session).verify(source_id)
                assert verified.outcome == "matched_transport"
                assert verified.successor_publication_id == legacy.publication_id
                assert verified.successor_schedule_entry_id == legacy.schedule_entry_id
                assert verified.successor_legacy_post_task_id == legacy.legacy_post_task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_verifier_fails_closed_on_canonical_successor_runtime_drift(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-verify-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_id = await _seed_reserved_repeat(
                Session,
                seed=3,
                scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            )

            async with Session() as session:
                created = await CanonicalRepeatSuccessorMaterializer(session).materialize(
                    source_id
                )
                assert created.outcome == "created"
                successor = await session.get(Publication, int(created.publication_id or 0))
                assert successor is not None
                successor.meta = {
                    **dict(successor.meta or {}),
                    "runtime_options": {"silent": False},
                }
                await session.commit()

                verified = await CanonicalRepeatSuccessorVerifier(session).verify(source_id)
                assert verified.outcome == "conflict"
                assert verified.successor_publication_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_verifier_fails_closed_when_exact_slot_has_multiple_successors(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-repeat-verify-duplicate.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_id = await _seed_reserved_repeat(
                Session,
                seed=4,
                scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            )

            async with Session() as session:
                created = await CanonicalRepeatSuccessorMaterializer(session).materialize(
                    source_id
                )
                assert created.outcome == "created"
                successor = await session.get(Publication, int(created.publication_id or 0))
                schedule = await session.get(
                    ScheduleEntry,
                    int(created.schedule_entry_id or 0),
                )
                assert successor is not None and schedule is not None
                duplicate_schedule = ScheduleEntry(
                    content_item_id=int(schedule.content_item_id),
                    content_revision=int(schedule.content_revision),
                    channel_id=int(schedule.channel_id),
                    scheduled_at=schedule.scheduled_at,
                    timezone=schedule.timezone,
                    status="pending",
                    repeat_rule=deepcopy(schedule.repeat_rule),
                    meta=deepcopy(schedule.meta),
                )
                duplicate = Publication(
                    schedule_entry_id=None,
                    content_item_id=int(successor.content_item_id),
                    content_revision=int(successor.content_revision),
                    channel_id=int(successor.channel_id),
                    status="queued",
                    legacy_post_task_id=None,
                    meta=deepcopy(successor.meta),
                )
                session.add_all([duplicate_schedule, duplicate])
                await session.flush()
                duplicate.schedule_entry_id = int(duplicate_schedule.id)
                await session.commit()

                verified = await CanonicalRepeatSuccessorVerifier(session).verify(source_id)
                assert verified.outcome == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())
