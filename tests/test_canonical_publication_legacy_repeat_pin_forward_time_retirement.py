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
    canonical_publication_delivery_repeat_time_pin_forward_started,
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed(Session, *, seed: int) -> tuple[int, int, tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=214000 + seed,
            username=f"repeat-pin-forward-time-{seed}",
            full_name=f"Repeat Pin Forward Time {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(101314000 + seed),
            title=f"Combined Time Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        first = Channel(
            tg_chat_id=-(101414000 + seed),
            title=f"Combined Time First {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        second = Channel(
            tg_chat_id=-(101514000 + seed),
            title=f"Combined Time Second {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, first, second])
        await session.flush()
        targets = (int(first.id), int(second.id))
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Combined time {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "pin_on": True,
                "forward_to": list(targets),
                "autodelete_seconds": 120,
            },
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id), targets


def _publish_primary(
    primary: _PrimaryWorker,
    *,
    pin: bool = True,
    forward: bool = True,
    combined: bool = True,
) -> None:
    set_canonical_publication_delivery_primary_worker(
        primary,
        time_autodelete_available=True,
        repeat_continuation_available=True,
        repeat_owner_policy_enforced=True,
        repeat_time_available=True,
        repeat_time_pin_available=pin,
        repeat_time_forward_available=forward,
        repeat_time_pin_forward_available=combined,
    )


async def _mark_one(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_combined_started_fact_requires_both_siblings_and_dedicated_bit() -> None:
    primary = _PrimaryWorker()
    try:
        _publish_primary(primary, pin=False, forward=True, combined=True)
        assert canonical_publication_delivery_repeat_time_pin_forward_started() is False
        _publish_primary(primary, pin=True, forward=False, combined=True)
        assert canonical_publication_delivery_repeat_time_pin_forward_started() is False
        _publish_primary(primary, pin=True, forward=True, combined=False)
        assert canonical_publication_delivery_repeat_time_pin_forward_started() is False
        _publish_primary(primary)
        assert canonical_publication_delivery_repeat_time_pin_forward_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_scheduler_yields_exact_combined_then_atomic_handoff_requires_combined_allow(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-pin-forward-time-yield.db'}"
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
                siblings_only = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="combined-siblings-only",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_time_autodelete=True,
                    allow_repeat_time=True,
                    allow_repeat_time_pin=True,
                    allow_repeat_time_forward=True,
                )
                assert siblings_only.outcome == "ineligible"

            async with Session() as canonical_session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="combined-retirement",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_time_autodelete=True,
                    allow_repeat_time=True,
                    allow_repeat_time_pin=True,
                    allow_repeat_time_forward=True,
                    allow_repeat_time_pin_forward=True,
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


def test_scheduler_keeps_combined_on_legacy_when_only_siblings_are_live(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-pin-forward-time-no-combined.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, _ = await _seed(Session, seed=2)
            _publish_primary(primary, combined=False)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_combined_on_legacy_when_one_sibling_is_missing(tmp_path) -> None:
    async def run() -> None:
        for seed, pin, forward in ((3, False, True), (4, True, False)):
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / f'repeat-pin-forward-time-sibling-{seed}.db'}"
            )
            primary = _PrimaryWorker()
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                Session = async_sessionmaker(engine, expire_on_commit=False)
                _, task_id, _ = await _seed(Session, seed=seed)
                _publish_primary(primary, pin=pin, forward=forward, combined=True)
                items = await _mark_one(Session, task_id)
                assert len(items) == 1 and items[0].status == "processing"
            finally:
                set_canonical_publication_delivery_primary_worker(None)
                await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_combined_on_legacy_when_order_drifts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-pin-forward-time-order-drift.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, targets = await _seed(Session, seed=5)
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
