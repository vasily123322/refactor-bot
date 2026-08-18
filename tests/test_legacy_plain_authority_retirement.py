from __future__ import annotations

import asyncio
import gc
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
    set_canonical_publication_delivery_primary_worker,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed_plain_publication(Session, *, seed: int) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    async with Session() as session:
        owner = Client(
            tg_user_id=194000 + seed,
            username=f"retirement-{seed}",
            full_name=f"Retirement {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(194100 + seed),
            title=f"Retirement {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Plain {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_primary_started_fact_is_bound_to_live_worker() -> None:
    set_canonical_publication_delivery_primary_worker(None)
    primary = _PrimaryWorker()
    set_canonical_publication_delivery_primary_worker(primary)
    assert canonical_publication_delivery_primary_started() is True

    del primary
    gc.collect()
    assert canonical_publication_delivery_primary_started() is False


def test_started_canonical_primary_retires_exact_plain_before_legacy_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'plain-authority.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_plain_publication(Session, seed=1)
            set_canonical_publication_delivery_primary_worker(primary)
            scheduler = Scheduler(
                Session,
                SimpleNamespace(),
                repeat_continuation_enabled=False,
            )

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                items = [task]
                await scheduler._mark_processing(scheduler_session, items)
                assert items == []

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_plain_transport_stays_legacy_owned_before_canonical_primary_start(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'plain-fallback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_plain_publication(Session, seed=2)
            set_canonical_publication_delivery_primary_worker(None)
            scheduler = Scheduler(
                Session,
                SimpleNamespace(),
                repeat_continuation_enabled=False,
            )

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                items = [task]
                await scheduler._mark_processing(scheduler_session, items)
                assert len(items) == 1
                assert items[0].status == "processing"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "processing"
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id == task_id
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_started_primary_preserves_legacy_fallback_for_hidden_pin_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-fallback.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_plain_publication(Session, seed=3)

            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                task.payload = {**dict(task.payload or {}), "pin_on": True}
                await drift_session.commit()

            set_canonical_publication_delivery_primary_worker(primary)
            scheduler = Scheduler(
                Session,
                SimpleNamespace(),
                repeat_continuation_enabled=False,
            )
            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                items = [task]
                await scheduler._mark_processing(scheduler_session, items)
                assert len(items) == 1
                assert items[0].status == "processing"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "processing"
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id == task_id
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())
