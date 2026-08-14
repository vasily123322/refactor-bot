from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.services.posting import PostingService
from app.workers import canonical_repeat_continuation_scheduler as scheduler_module


class _Bot:
    pass


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _channel(Session, suffix: int) -> Channel:
    async with Session() as session:
        owner = Client(
            tg_user_id=9_970_000 + suffix,
            username=f"proof{suffix}",
            full_name="Scheduler Proof Fixture",
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_970_000_000 - suffix,
            title=f"Proof {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


async def _linked_task(Session, suffix: int, *, repeat: bool = False) -> int:
    channel = await _channel(Session, suffix)
    payload = {"type": "text", "text": f"linked proof {suffix}"}
    if repeat:
        payload["repeat_on"] = True
        payload["repeat_seconds"] = 3600
    task = await PostingService(_Bot(), Session).schedule(
        int(channel.id),
        payload,
        datetime.now(timezone.utc) + timedelta(minutes=30),
        dedupe_key=f"linked-proof-{suffix}",
    )
    return int(task.id)


async def _unlinked_task(Session, suffix: int, *, repeat: bool = False) -> int:
    channel = await _channel(Session, suffix)
    payload = {
        "type": "unsupported_scheduler_proof_fixture",
        "text": f"unlinked proof {suffix}",
    }
    if repeat:
        payload["repeat_on"] = True
        payload["repeat_seconds"] = 3600
    async with Session() as session:
        task = PostTask(
            channel_id=int(channel.id),
            status="pending",
            payload=payload,
            dedupe_key=f"unlinked-proof-{suffix}",
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return int(task.id)


def _scheduler(Session):
    return scheduler_module.Scheduler(
        Session,
        PostingService(_Bot(), Session),
        repeat_continuation_enabled=False,
    )


def test_linked_proof_exception_cannot_fall_through_to_legacy_processing() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        original_started = scheduler_module.canonical_publication_delivery_primary_started
        try:
            task_id = await _linked_task(Session, 1)
            scheduler_module.canonical_publication_delivery_primary_started = lambda: True
            scheduler = _scheduler(Session)

            async def no_repeat(session, *, task_id: int) -> bool:
                return False

            async def proof_failure(session, *, task_id: int) -> bool:
                raise RuntimeError("canonical proof unavailable")

            scheduler._yield_proven_repeat_to_canonical_primary = no_repeat  # type: ignore[method-assign]
            scheduler._yield_proven_nonrepeat_to_canonical_primary = proof_failure  # type: ignore[method-assign]

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                items = [task]
                await scheduler._mark_processing(session, items)
                assert items == []

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                assert task.status == "pending"
        finally:
            scheduler_module.canonical_publication_delivery_primary_started = original_started
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_proof_exception_preserves_intentional_legacy_processing() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        original_started = scheduler_module.canonical_publication_delivery_primary_started
        try:
            task_id = await _unlinked_task(Session, 2)
            scheduler_module.canonical_publication_delivery_primary_started = lambda: True
            scheduler = _scheduler(Session)

            async def no_repeat(session, *, task_id: int) -> bool:
                return False

            async def proof_failure(session, *, task_id: int) -> bool:
                raise RuntimeError("canonical proof unavailable")

            scheduler._yield_proven_repeat_to_canonical_primary = no_repeat  # type: ignore[method-assign]
            scheduler._yield_proven_nonrepeat_to_canonical_primary = proof_failure  # type: ignore[method-assign]

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                items = [task]
                await scheduler._mark_processing(session, items)
                assert [int(item.id) for item in items] == [task_id]

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                assert task.status == "processing"
        finally:
            scheduler_module.canonical_publication_delivery_primary_started = original_started
            await engine.dispose()

    asyncio.run(run())


def test_linked_repeat_boot_proof_exception_blocks_legacy_mutation() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        original_started = scheduler_module.canonical_publication_delivery_repeat_started
        try:
            task_id = await _linked_task(Session, 3, repeat=True)
            scheduler_module.canonical_publication_delivery_repeat_started = lambda: True
            scheduler = _scheduler(Session)

            async def proof_failure(session, *, task_id: int) -> bool:
                raise RuntimeError("repeat ownership proof unavailable")

            scheduler._yield_proven_repeat_to_canonical_primary = proof_failure  # type: ignore[method-assign]
            async with Session() as session:
                assert await scheduler._legacy_repeat_boot_mutation_protected(
                    session,
                    task_id=task_id,
                ) is True
        finally:
            scheduler_module.canonical_publication_delivery_repeat_started = original_started
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_repeat_boot_proof_exception_keeps_legacy_fallback() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        original_started = scheduler_module.canonical_publication_delivery_repeat_started
        try:
            task_id = await _unlinked_task(Session, 4, repeat=True)
            scheduler_module.canonical_publication_delivery_repeat_started = lambda: True
            scheduler = _scheduler(Session)

            async def proof_failure(session, *, task_id: int) -> bool:
                raise RuntimeError("repeat ownership proof unavailable")

            scheduler._yield_proven_repeat_to_canonical_primary = proof_failure  # type: ignore[method-assign]
            async with Session() as session:
                assert await scheduler._legacy_repeat_boot_mutation_protected(
                    session,
                    task_id=task_id,
                ) is False
        finally:
            scheduler_module.canonical_publication_delivery_repeat_started = original_started
            await engine.dispose()

    asyncio.run(run())


def test_linkage_lookup_error_after_proof_failure_is_fail_closed() -> None:
    class _BrokenSession:
        async def scalar(self, statement):
            raise RuntimeError("linkage unavailable")

        async def rollback(self):
            return None

    async def run() -> None:
        scheduler = object.__new__(scheduler_module.Scheduler)
        assert await scheduler._linked_after_canonical_proof_error(  # type: ignore[arg-type]
            _BrokenSession(),
            task_id=123,
        ) is True

    asyncio.run(run())
