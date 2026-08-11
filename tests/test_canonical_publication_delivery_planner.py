from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


_RUNTIME_OPTIONS = {
    "silent": True,
    "pin_on": False,
    "autodelete_seconds": 3600,
    "nested": {"mode": "stable"},
}


async def _seed_queued_publication(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=117000 + seed,
            username=f"canonical-delivery-{seed}",
            full_name=f"Canonical Delivery {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(117100 + seed),
            title=f"Canonical Delivery {seed}",
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
                        "text": "Canonical executor proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        return (
            int(publication.id),
            int(publication.legacy_post_task_id or 0),
            int(channel.id),
            int(channel.tg_chat_id),
        )


def test_due_publication_plans_after_physical_post_task_retirement(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-retired.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id, channel_id, telegram_chat_id = (
                await _seed_queued_publication(
                    Session,
                    seed=1,
                    scheduled_at=scheduled_at,
                )
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                plan = await CanonicalPublicationDeliveryPlanner(session).plan(
                    publication_id,
                    at=scheduled_at + timedelta(minutes=5),
                )

                assert plan is not None
                assert plan.publication_id == publication_id
                assert plan.channel_id == channel_id
                assert plan.telegram_chat_id == telegram_chat_id
                assert plan.scheduled_at == scheduled_at
                assert plan.runtime_options() == _RUNTIME_OPTIONS
                document = plan.post_document()
                assert document.blocks[0].text == "Canonical executor proof"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_future_or_inactive_publication_is_not_delivery_eligible(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-due.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _task_id, channel_id, _chat_id = await _seed_queued_publication(
                Session,
                seed=2,
                scheduled_at=scheduled_at,
            )

            async with Session() as session:
                planner = CanonicalPublicationDeliveryPlanner(session)
                assert await planner.plan(
                    publication_id,
                    at=scheduled_at - timedelta(seconds=1),
                ) is None

                channel = await session.get(Channel, channel_id)
                assert channel is not None
                channel.is_active = False
                await session.commit()
                assert await planner.plan(
                    publication_id,
                    at=scheduled_at + timedelta(minutes=1),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_runtime_intent_drift_between_publication_and_schedule_fails_closed(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-runtime-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _task_id, _channel_id, _chat_id = (
                await _seed_queued_publication(
                    Session,
                    seed=3,
                    scheduled_at=scheduled_at,
                )
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"autodelete_seconds": 999},
                }
                await session.commit()

                assert await CanonicalPublicationDeliveryPlanner(session).plan(
                    publication_id,
                    at=scheduled_at + timedelta(minutes=1),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_attempt_or_delivery_evidence_blocks_new_delivery_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-delivery-attempt.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, _task_id, _channel_id, _chat_id = (
                await _seed_queued_publication(
                    Session,
                    seed=4,
                    scheduled_at=scheduled_at,
                )
            )

            async with Session() as session:
                session.add(
                    PublicationAttempt(
                        publication_id=publication_id,
                        attempt=1,
                        status="processing",
                        telegram_message_ids=None,
                        error=None,
                        meta={},
                    )
                )
                await session.commit()
                planner = CanonicalPublicationDeliveryPlanner(session)
                assert await planner.plan(
                    publication_id,
                    at=scheduled_at + timedelta(minutes=1),
                ) is None

                attempt = await session.get(PublicationAttempt, 1)
                assert attempt is not None
                await session.delete(attempt)
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.telegram_message_ids = [123]
                await session.commit()
                assert await planner.plan(
                    publication_id,
                    at=scheduled_at + timedelta(minutes=1),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
