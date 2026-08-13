from __future__ import annotations

import asyncio
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
    canonical_publication_delivery_repeat_time_forward_started,
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed(Session, *, seed: int, pin_on: bool = False) -> tuple[int, int, tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=213000 + seed,
            username=f"repeat-forward-time-retirement-{seed}",
            full_name=f"Repeat Forward Time Retirement {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(101013000 + seed),
            title=f"Repeat Forward Time Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        first = Channel(
            tg_chat_id=-(101113000 + seed),
            title=f"Repeat Forward Time First {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        second = Channel(
            tg_chat_id=-(101213000 + seed),
            title=f"Repeat Forward Time Second {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, first, second])
        await session.flush()
        targets = (int(first.id), int(second.id))
        options = {
            "autodelete_seconds": 120,
            "forward_to": list(targets),
        }
        if pin_on:
            options["pin_on"] = True
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Forward time {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id), targets


def _publish_primary(
    primary: _PrimaryWorker,
    *,
    repeat_time_available: bool = True,
    repeat_time_forward_available: bool = True,
) -> None:
    set_canonical_publication_delivery_primary_worker(
        primary,
        time_autodelete_available=True,
        repeat_continuation_available=True,
        repeat_owner_policy_enforced=True,
        repeat_time_available=repeat_time_available,
        repeat_time_forward_available=repeat_time_forward_available,
    )


async def _mark_one(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_repeat_forward_time_authority_requires_plain_and_dedicated_started_facts() -> None:
    primary = _PrimaryWorker()
    try:
        _publish_primary(primary, repeat_time_available=False)
        assert canonical_publication_delivery_repeat_time_forward_started() is False
        _publish_primary(primary, repeat_time_forward_available=False)
        assert canonical_publication_delivery_repeat_time_forward_started() is False
        _publish_primary(primary)
        assert canonical_publication_delivery_repeat_time_forward_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_scheduler_yields_exact_ordered_repeat_forward_time_then_atomic_handoff(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-forward-time-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(Session, seed=1)
            _publish_primary(primary)

            assert await _mark_one(Session, task_id) == []
            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.status == "queued"
                assert publication.legacy_post_task_id == task_id

            async with Session() as canonical_session:
                insufficient = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-forward-time-insufficient",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_time_autodelete=True,
                    allow_repeat_time=True,
                )
                assert insufficient.outcome == "ineligible"

            async with Session() as canonical_session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-forward-time-retirement",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_time_autodelete=True,
                    allow_repeat_time=True,
                    allow_repeat_time_forward=True,
                )
                assert transfer.outcome == "claimed"
                assert transfer.claim is not None

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None and publication.status == "sending"
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_repeat_forward_time_on_legacy_without_dedicated_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-forward-time-no-dedicated.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, _ = await _seed(Session, seed=2)
            _publish_primary(primary, repeat_time_forward_available=False)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_repeat_forward_time_on_legacy_when_order_drifts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-forward-time-order-drift.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, targets = await _seed(Session, seed=3)
            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["forward_to"] = [targets[1], targets[0]]
                task.payload = payload
                await drift_session.commit()
            _publish_primary(primary)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_combined_pin_forward_time_on_legacy(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-forward-time-combined.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, _ = await _seed(Session, seed=4, pin_on=True)
            _publish_primary(primary)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())
