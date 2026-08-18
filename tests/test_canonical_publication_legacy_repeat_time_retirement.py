from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_repeat_time_started,
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
    with_forward_target: bool = False,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=211000 + seed,
            username=f"repeat-time-retirement-{seed}",
            full_name=f"Repeat Time Retirement {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100611000 + seed),
            title=f"Repeat Time Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(source)
        await session.flush()

        options = dict(runtime_options or {})
        if with_forward_target:
            target = Channel(
                tg_chat_id=-(100711000 + seed),
                title=f"Repeat Time Forward Target {seed}",
                owner_id=int(owner.id),
                is_active=True,
            )
            session.add(target)
            await session.flush()
            options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Repeat time retirement {seed}",
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


def _publish_primary(
    primary: _PrimaryWorker,
    *,
    time_autodelete_available: bool,
    repeat_time_available: bool = True,
) -> None:
    set_canonical_publication_delivery_primary_worker(
        primary,
        time_autodelete_available=time_autodelete_available,
        repeat_continuation_available=True,
        repeat_owner_policy_enforced=True,
        repeat_time_available=repeat_time_available,
    )


async def _mark_one(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(
        Session,
        SimpleNamespace(),
        repeat_continuation_enabled=False,
    )
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_repeat_time_authority_requires_dedicated_started_dependency() -> None:
    primary = _PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(
            primary,
            time_autodelete_available=True,
            repeat_continuation_available=True,
            repeat_owner_policy_enforced=True,
        )
        assert canonical_publication_delivery_repeat_time_started() is False

        _publish_primary(
            primary,
            time_autodelete_available=True,
            repeat_time_available=True,
        )
        assert canonical_publication_delivery_repeat_time_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)
    assert canonical_publication_delivery_repeat_time_started() is False


def test_scheduler_yields_repeat_time_only_with_live_time_fact_and_strict_claim_flags(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                runtime_options={"silent": True, "autodelete_seconds": 120},
            )
            _publish_primary(primary, time_autodelete_available=True)

            items = await _mark_one(Session, task_id)
            assert items == []
            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None and publication.status == "queued"
                assert publication.legacy_post_task_id == task_id

            async with Session() as canonical_session:
                no_time = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-time-no-time",
                    ttl_seconds=180,
                    allow_repeat=True,
                )
                assert no_time.outcome == "ineligible"

            async with Session() as canonical_session:
                no_repeat_time = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-time-no-repeat-time",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_time_autodelete=True,
                )
                assert no_repeat_time.outcome == "ineligible"

            async with Session() as canonical_session:
                transfer = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    canonical_session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-time-retirement",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_time_autodelete=True,
                    allow_repeat_time=True,
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


def test_scheduler_keeps_repeat_time_on_legacy_without_live_repeat_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-no-repeat.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=2,
                runtime_options={"autodelete_seconds": 120},
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                time_autodelete_available=True,
                repeat_time_available=True,
            )
            items = await _mark_one(Session, task_id)
            assert len(items) == 1
            assert items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_repeat_time_on_legacy_without_live_time_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-no-time.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=3,
                runtime_options={"autodelete_seconds": 120},
            )
            _publish_primary(primary, time_autodelete_available=False)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1
            assert items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_repeat_time_on_legacy_without_dedicated_repeat_time_fact(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-no-dedicated.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=6,
                runtime_options={"autodelete_seconds": 120},
            )
            _publish_primary(
                primary,
                time_autodelete_available=True,
                repeat_time_available=False,
            )
            items = await _mark_one(Session, task_id)
            assert len(items) == 1
            assert items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_keeps_repeat_time_on_legacy_when_seconds_drift(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-drift.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=4,
                runtime_options={"autodelete_seconds": 120},
            )
            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["autodelete_seconds"] = 121
                task.payload = payload
                await drift_session.commit()

            _publish_primary(primary, time_autodelete_available=True)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1
            assert items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_scheduler_yields_repeat_time_with_explicit_false_report(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-false-report.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id = await _seed(
                Session,
                seed=5,
                runtime_options={
                    "autodelete_seconds": 120,
                    "autodelete_report": False,
                },
            )
            _publish_primary(primary, time_autodelete_available=True)
            assert await _mark_one(Session, task_id) == []
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("seed", "runtime_options", "with_forward_target"),
    [
        (10, {"autodelete_seconds": 120, "pin_on": True}, False),
        (11, {"autodelete_seconds": 120}, True),
        (12, {"autodelete_seconds": 120, "autodelete_report": True}, False),
        (13, {"autodelete_seconds": 120, "autodelete_views": 50}, False),
    ],
)
def test_scheduler_keeps_other_destructive_repeat_time_profiles_on_legacy(
    tmp_path,
    seed: int,
    runtime_options: dict,
    with_forward_target: bool,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'repeat-time-composed-{seed}.db'}"
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
                with_forward_target=with_forward_target,
            )
            _publish_primary(primary, time_autodelete_available=True)
            items = await _mark_one(Session, task_id)
            assert len(items) == 1
            assert items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())
