from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.domain.scheduler import SchedulerTaskLease
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimService,
)
from app.services.canonical_publication_delivery_authority import (
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_nonrepeat_authority import (
    canonical_publication_delivery_nonrepeat_plain_started,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed(
    Session,
    *,
    seed: int,
    runtime_options: dict | None = None,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=219000 + seed,
            username=f"nonrepeat-plain-{seed}",
            full_name=f"Nonrepeat Plain {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(102419000 + seed),
            title=f"Nonrepeat Plain {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Nonrepeat plain {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=runtime_options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


async def _mark(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_plain_started_tracks_successfully_started_primary() -> None:
    primary = _PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(None)
        assert canonical_publication_delivery_nonrepeat_plain_started() is False
        set_canonical_publication_delivery_primary_worker(primary)
        assert canonical_publication_delivery_nonrepeat_plain_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_scheduler_plain_yield_is_read_only_before_atomic_handoff(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'nonrepeat-plain-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(Session, seed=1)
            set_canonical_publication_delivery_primary_worker(primary)

            assert await _mark(Session, task_id) == []

            async with Session() as check:
                task = await check.get(PostTask, task_id)
                publication = await check.get(Publication, publication_id)
                scheduler_leases = list(
                    (
                        await check.execute(
                            select(SchedulerTaskLease).where(
                                SchedulerTaskLease.task_id == task_id
                            )
                        )
                    ).scalars().all()
                )
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task_id
                assert scheduler_leases == []

            async with Session() as claim_session:
                claimed = await CanonicalPublicationAtomicHandoffClaimService(
                    claim_session
                ).claim_linked(
                    publication_id,
                    holder="nonrepeat-plain-canonical",
                    ttl_seconds=180,
                )
                assert claimed.outcome == "claimed"

            async with Session() as check:
                assert await check.get(PostTask, task_id) is None
                publication = await check.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_plain_missing_fact_falls_back_to_legacy(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'nonrepeat-plain-fact-missing.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(Session, seed=2)
            set_canonical_publication_delivery_primary_worker(primary)
            monkeypatch.setattr(
                "app.services.canonical_publication_nonrepeat_scheduler_proof."
                "canonical_publication_delivery_nonrepeat_plain_started",
                lambda: False,
            )

            items = await _mark(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"

            async with Session() as check:
                lease = (
                    await check.execute(
                        select(SchedulerTaskLease).where(
                            SchedulerTaskLease.task_id == task_id
                        )
                    )
                ).scalar_one_or_none()
                assert lease is not None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_plain_slice_keeps_pin_profile_on_legacy(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'nonrepeat-plain-pin-closed.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=3,
                runtime_options={"pin_on": True},
            )
            set_canonical_publication_delivery_primary_worker(primary)

            items = await _mark(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())
