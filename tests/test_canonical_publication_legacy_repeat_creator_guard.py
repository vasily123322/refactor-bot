from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


async def _seed(Session, *, seed: int, count: int = 1) -> list[int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=231000 + seed,
            username=f"repeat-guard-{seed}",
            full_name="Repeat Guard",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(104631000 + seed),
            title="repeat guard",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        ids: list[int] = []
        group_id = 900000 + seed
        for offset in range(count):
            post = PostTask(
                channel_id=int(channel.id),
                payload={
                    "text": f"guard {offset}",
                    "repeat_on": True,
                    "repeat_seconds": 3600,
                    "repeat_group_id": group_id,
                },
                scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5 - offset),
                status="pending",
            )
            session.add(post)
            await session.flush()
            ids.append(int(post.id))
        await session.commit()
        return ids


async def _rows(Session) -> list[PostTask]:
    async with Session() as session:
        return list(
            (await session.execute(select(PostTask).order_by(PostTask.id))).scalars().all()
        )


def _protect_all(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.workers.canonical_repeat_continuation_scheduler."
        "canonical_publication_delivery_repeat_started",
        lambda: True,
    )

    async def proven(self, session, *, task_id: int) -> bool:
        return True

    monkeypatch.setattr(Scheduler, "_yield_proven_repeat_to_canonical_primary", proven)


def test_boot_cleanup_never_creates_or_skips_unique_canonical_owned_repeat(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-guard.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            [task_id] = await _seed(Session, seed=1)
            _protect_all(monkeypatch)
            scheduler = Scheduler(
                Session, SimpleNamespace(), repeat_continuation_enabled=False
            )
            scheduler._boot_time = datetime.now(timezone.utc)
            scheduler._boot_cleanup_done = False
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                remaining = await scheduler._boot_cleanup_repeats(session, [task])
                assert [int(post.id) for post in remaining] == [task_id]
            rows = await _rows(Session)
            assert len(rows) == 1
            assert rows[0].status == "pending"
            assert dict(rows[0].payload or {}).get("repeat_on") is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_overflow_ambiguous_canonical_group_falls_back_to_legacy_dedupe(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-overflow-guard.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            ids = await _seed(Session, seed=2, count=3)
            _protect_all(monkeypatch)
            monkeypatch.setattr(
                "app.workers.canonical_repeat_continuation_scheduler.settings.repeat_overflow_limit",
                1,
            )
            scheduler = Scheduler(
                Session, SimpleNamespace(), repeat_continuation_enabled=False
            )
            async with Session() as session:
                first = await session.get(PostTask, ids[0])
                assert first is not None
                await scheduler._prevent_repeat_overflow(session, [first])
            rows = await _rows(Session)
            assert [row.status for row in rows] == ["skipped", "skipped", "pending"]
            assert dict(rows[-1].payload or {}).get("repeat_on") is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mixed_repeat_group_fails_closed_to_legacy_boot_cleanup(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-mixed-boot-fallback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            ids = await _seed(Session, seed=3, count=2)
            monkeypatch.setattr(
                "app.workers.canonical_repeat_continuation_scheduler."
                "canonical_publication_delivery_repeat_started",
                lambda: True,
            )

            async def first_only(self, session, *, task_id: int) -> bool:
                return int(task_id) == ids[0]

            monkeypatch.setattr(
                Scheduler, "_yield_proven_repeat_to_canonical_primary", first_only
            )
            scheduler = Scheduler(
                Session, SimpleNamespace(), repeat_continuation_enabled=False
            )
            scheduler._boot_time = datetime.now(timezone.utc)
            scheduler._boot_cleanup_done = False
            async with Session() as session:
                selected = [
                    task
                    for task_id in ids
                    if (task := await session.get(PostTask, task_id)) is not None
                ]
                remaining = await scheduler._boot_cleanup_repeats(session, selected)
                assert remaining == []
            rows = await _rows(Session)
            assert len(rows) == 3
            assert [row.status for row in rows[:2]] == ["skipped", "skipped"]
            assert rows[2].status == "pending"
            assert dict(rows[2].payload or {}).get("repeat_on") is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unproven_repeat_still_uses_legacy_boot_cleanup(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-fallback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            [task_id] = await _seed(Session, seed=4)
            monkeypatch.setattr(
                "app.workers.canonical_repeat_continuation_scheduler."
                "canonical_publication_delivery_repeat_started",
                lambda: True,
            )

            async def not_proven(self, session, *, task_id: int) -> bool:
                return False

            monkeypatch.setattr(
                Scheduler, "_yield_proven_repeat_to_canonical_primary", not_proven
            )
            scheduler = Scheduler(
                Session, SimpleNamespace(), repeat_continuation_enabled=False
            )
            scheduler._boot_time = datetime.now(timezone.utc)
            scheduler._boot_cleanup_done = False
            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                remaining = await scheduler._boot_cleanup_repeats(session, [task])
                assert remaining == []
            rows = await _rows(Session)
            assert len(rows) == 2
            assert rows[0].status == "skipped"
            assert rows[1].status == "pending"
        finally:
            await engine.dispose()

    asyncio.run(run())
