from __future__ import annotations

import asyncio

from sqlalchemy import UniqueConstraint, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import PostingDedupeLock, Publication
from app.services.posting import PostingService
import app.services.canonical_schedule_materializer as materializer_module


class _Bot:
    pass


async def _new_db(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _channel(Session, suffix: int) -> Channel:
    async with Session() as session:
        owner = Client(
            tg_user_id=9_970_000 + suffix,
            username=f"dedupe{suffix}",
            full_name="Dedupe Fixture",
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_970_000_000 - suffix,
            title=f"Dedupe {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


def _serialized_fake_lock(monkeypatch):
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    first_committed = asyncio.Event()
    calls = 0

    async def acquire(session, dedupe_key: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            await release_first.wait()
            return
        second_entered.set()
        await first_committed.wait()

    monkeypatch.setattr(materializer_module, "acquire_posting_dedupe_lock", acquire)
    return first_entered, second_entered, release_first, first_committed


def test_direct_publication_keeps_db_unique_dedupe_and_shared_mutex_identity() -> None:
    unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in Publication.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique["uq_publication_posting_dedupe"] == ("posting_dedupe_key",)
    assert tuple(PostingDedupeLock.__table__.primary_key.columns.keys()) == (
        "dedupe_key",
    )


def test_cross_mode_race_canonical_winner_is_reused(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "canonical-winner.db")
        try:
            channel = await _channel(Session, 1)
            (
                first_entered,
                second_entered,
                release_first,
                first_committed,
            ) = _serialized_fake_lock(monkeypatch)
            service = PostingService(_Bot(), Session)
            key = "cross-mode-canonical-winner"

            winner_task = asyncio.create_task(
                service.schedule(
                    int(channel.id),
                    {"type": "text", "text": "canonical winner"},
                    None,
                    dedupe_key=key,
                )
            )
            await first_entered.wait()
            loser_task = asyncio.create_task(
                service.schedule(
                    int(channel.id),
                    {
                        "type": "text",
                        "text": "legacy contender",
                        "autodelete_seconds": 60,
                        "autodelete_views": 10,
                    },
                    None,
                    dedupe_key=key,
                )
            )
            await second_entered.wait()
            release_first.set()
            winner = await winner_task
            first_committed.set()
            loser = await loser_task

            assert isinstance(winner, Publication)
            assert isinstance(loser, Publication)
            assert int(loser.id) == int(winner.id)
            async with Session() as session:
                publications = list(
                    (await session.execute(select(Publication))).scalars().all()
                )
                tasks = list((await session.execute(select(PostTask))).scalars().all())
            assert [int(row.id) for row in publications] == [int(winner.id)]
            assert tasks == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cross_mode_race_legacy_winner_is_reused(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "legacy-winner.db")
        try:
            channel = await _channel(Session, 2)
            (
                first_entered,
                second_entered,
                release_first,
                first_committed,
            ) = _serialized_fake_lock(monkeypatch)
            service = PostingService(_Bot(), Session)
            key = "cross-mode-legacy-winner"

            winner_task = asyncio.create_task(
                service.schedule(
                    int(channel.id),
                    {
                        "type": "text",
                        "text": "legacy winner",
                        "autodelete_seconds": 60,
                        "autodelete_views": 10,
                    },
                    None,
                    dedupe_key=key,
                )
            )
            await first_entered.wait()
            loser_task = asyncio.create_task(
                service.schedule(
                    int(channel.id),
                    {"type": "text", "text": "canonical contender"},
                    None,
                    dedupe_key=key,
                )
            )
            await second_entered.wait()
            release_first.set()
            winner = await winner_task
            first_committed.set()
            loser = await loser_task

            assert isinstance(winner, PostTask)
            assert isinstance(loser, PostTask)
            assert int(loser.id) == int(winner.id)
            async with Session() as session:
                tasks = list((await session.execute(select(PostTask))).scalars().all())
                publications = list(
                    (await session.execute(select(Publication))).scalars().all()
                )
            assert [int(row.id) for row in tasks] == [int(winner.id)]
            assert len(publications) == 1
            assert publications[0].legacy_post_task_id == int(winner.id)
        finally:
            await engine.dispose()

    asyncio.run(run())
