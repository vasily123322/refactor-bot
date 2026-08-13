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
    canonical_publication_delivery_repeat_started,
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
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
            tg_user_id=209000 + seed,
            username=f"plain-repeat-retirement-{seed}",
            full_name=f"Plain Repeat Retirement {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100209000 + seed),
            title=f"Plain Repeat Retirement {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()

        options = dict(runtime_options or {})
        forward_target_count = int(options.pop("_forward_target_count", 0))
        if options.pop("_with_forward_target", False):
            forward_target_count = max(forward_target_count, 1)
        if forward_target_count:
            target_ids: list[int] = []
            for index in range(forward_target_count):
                target = Channel(
                    tg_chat_id=-(100309000 + seed * 10 + index),
                    title=f"Plain Repeat Forward Target {seed}-{index}",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add(target)
                await session.flush()
                target_ids.append(int(target.id))
            options["forward_to"] = target_ids

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Plain repeat retirement {seed}",
                    }
                ]
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
        return int(publication.id), int(publication.legacy_post_task_id)


def test_repeat_authority_requires_started_continuation_and_owner_policy() -> None:
    worker = _PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(None)
        assert canonical_publication_delivery_repeat_started() is False

        set_canonical_publication_delivery_primary_worker(
            worker,
            repeat_continuation_available=True,
        )
        assert canonical_publication_delivery_repeat_started() is False

        set_canonical_publication_delivery_primary_worker(
            worker,
            repeat_continuation_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert canonical_publication_delivery_repeat_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)
    assert canonical_publication_delivery_repeat_started() is False


def test_scheduler_yields_plain_repeat_then_canonical_atomic_handoff_claims(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'plain-repeat-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                runtime_options={"silent": True},
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                repeat_continuation_available=True,
                repeat_owner_policy_enforced=True,
            )
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
                task_after = await scheduler_session.get(
                    PostTask,
                    task_id,
                    populate_existing=True,
                )
                publication = await scheduler_session.get(
                    Publication,
                    publication_id,
                    populate_existing=True,
                )
                assert task_after is not None and task_after.status == "pending"
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task_id

            async with Session() as canonical_session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="plain-repeat-retirement",
                    ttl_seconds=180,
                    allow_repeat=True,
                )
                assert transfer.outcome == "claimed"
                assert transfer.claim is not None

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_yields_pin_repeat_then_canonical_atomic_handoff_claims(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-repeat-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=20,
                runtime_options={"silent": True, "pin_on": True},
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                repeat_continuation_available=True,
                repeat_owner_policy_enforced=True,
            )
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
                task_after = await scheduler_session.get(
                    PostTask,
                    task_id,
                    populate_existing=True,
                )
                publication = await scheduler_session.get(
                    Publication,
                    publication_id,
                    populate_existing=True,
                )
                assert task_after is not None and task_after.status == "pending"
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task_id

            async with Session() as canonical_session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="pin-repeat-retirement",
                    ttl_seconds=180,
                    allow_repeat=True,
                )
                assert transfer.outcome == "claimed"
                assert transfer.claim is not None

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_yields_forward_repeat_then_canonical_atomic_handoff_claims(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-repeat-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=30,
                runtime_options={"silent": True, "_forward_target_count": 2},
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                repeat_continuation_available=True,
                repeat_owner_policy_enforced=True,
            )
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
                task_after = await scheduler_session.get(
                    PostTask,
                    task_id,
                    populate_existing=True,
                )
                publication = await scheduler_session.get(
                    Publication,
                    publication_id,
                    populate_existing=True,
                )
                assert task_after is not None and task_after.status == "pending"
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task_id

            async with Session() as canonical_session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="forward-repeat-retirement",
                    ttl_seconds=180,
                    allow_repeat=True,
                )
                assert transfer.outcome == "claimed"
                assert transfer.claim is not None

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_plain_repeat_on_legacy_without_live_repeat_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'plain-repeat-fallback.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=2,
                runtime_options={"silent": True},
            )
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
                assert publication.legacy_post_task_id == task_id
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_pin_repeat_on_legacy_without_live_repeat_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-repeat-fallback.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=21,
                runtime_options={"pin_on": True},
            )
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
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_forward_repeat_on_legacy_without_live_repeat_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-repeat-fallback.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=31,
                runtime_options={"_forward_target_count": 2},
            )
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
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_pin_repeat_on_legacy_when_parity_drifts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-repeat-drift.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=22,
                runtime_options={"pin_on": True},
            )
            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["pin_on"] = False
                task.payload = payload
                await drift_session.commit()

            set_canonical_publication_delivery_primary_worker(
                primary,
                repeat_continuation_available=True,
                repeat_owner_policy_enforced=True,
            )
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
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_forward_repeat_on_legacy_when_target_order_drifts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-repeat-order-drift.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=32,
                runtime_options={"_forward_target_count": 2},
            )
            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                forward_to = list(payload.get("forward_to") or [])
                assert len(forward_to) == 2
                payload["forward_to"] = list(reversed(forward_to))
                task.payload = payload
                await drift_session.commit()

            set_canonical_publication_delivery_primary_worker(
                primary,
                repeat_continuation_available=True,
                repeat_owner_policy_enforced=True,
            )
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
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_unretired_repeat_profiles_on_legacy(tmp_path) -> None:
    async def assert_legacy(*, seed: int, runtime_options: dict, suffix: str) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'plain-repeat-{suffix}.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=seed,
                runtime_options=runtime_options,
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                repeat_continuation_available=True,
                repeat_owner_policy_enforced=True,
            )
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
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    async def run() -> None:
        await assert_legacy(
            seed=4,
            runtime_options={"autodelete_seconds": 60},
            suffix="time",
        )
        await assert_legacy(
            seed=5,
            runtime_options={"autodelete_views": 100},
            suffix="views",
        )
        await assert_legacy(
            seed=6,
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
            },
            suffix="views-report",
        )
        await assert_legacy(
            seed=23,
            runtime_options={"pin_on": True, "_with_forward_target": True},
            suffix="pin-forward",
        )
        await assert_legacy(
            seed=24,
            runtime_options={"pin_on": True, "autodelete_seconds": 60},
            suffix="pin-time",
        )
        await assert_legacy(
            seed=25,
            runtime_options={"pin_on": True, "autodelete_views": 100},
            suffix="pin-views",
        )
        await assert_legacy(
            seed=26,
            runtime_options={"pin_on": False},
            suffix="neutral-pin-key",
        )
        await assert_legacy(
            seed=33,
            runtime_options={"_with_forward_target": True, "autodelete_seconds": 60},
            suffix="forward-time",
        )
        await assert_legacy(
            seed=34,
            runtime_options={"_with_forward_target": True, "autodelete_views": 100},
            suffix="forward-views",
        )
        await assert_legacy(
            seed=35,
            runtime_options={
                "_with_forward_target": True,
                "autodelete_views": 100,
                "autodelete_report": True,
            },
            suffix="forward-views-report",
        )

    asyncio.run(run())
