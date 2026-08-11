from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.config import Settings
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import (
    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY,
)
from app.services.canonical_repeat_reservation_verifier import (
    CanonicalRepeatReservationVerifier,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers import canonical_scheduler as scheduler_module
from app.workers.canonical_scheduler import Scheduler


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


async def _seed_repeat(Session, *, seed: int) -> tuple[int, int, datetime]:
    async with Session() as session:
        owner = Client(
            tg_user_id=99900 + seed,
            username=f"repeat-shadow-{seed}",
            full_name=f"Repeat Shadow {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10099900 + seed),
            title=f"Repeat Shadow {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Shadow repeat"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        scheduled_at = datetime.now(timezone.utc) + timedelta(hours=2)
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
            },
        )
        task_id = int(publication.legacy_post_task_id or 0)
        task = await session.get(PostTask, task_id)
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [100000 + seed],
            "result_link": f"https://t.me/c/{99900 + seed}/{100000 + seed}",
        }
        await session.commit()
        return int(publication.id), task_id, scheduled_at


async def _repeat_child(session, *, root_task_id: int) -> PostTask:
    child = (
        await session.execute(
            select(PostTask)
            .where(
                PostTask.id != int(root_task_id),
                PostTask.status == "pending",
                PostTask.payload["repeat_group_id"].as_integer() == int(root_task_id),
            )
            .order_by(PostTask.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    assert child is not None
    return child


def test_shadow_setting_is_default_off_and_explicitly_configurable() -> None:
    assert _settings().canonical_repeat_shadow_planning_enabled is False
    assert (
        _settings(
            CANONICAL_REPEAT_SHADOW_PLANNING_ENABLED=True
        ).canonical_repeat_shadow_planning_enabled
        is True
    )


def test_enabled_shadow_reserves_then_matches_unchanged_legacy_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-shadow-enabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, scheduled_at = await _seed_repeat(Session, seed=1)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_shadow_planning=True,
                )
                await scheduler._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                reservation = publication.meta[
                    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY
                ]
                assert reservation == schedule.meta[
                    CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY
                ]
                assert reservation["scheduled_at"] == (
                    scheduled_at + timedelta(hours=1)
                ).isoformat()
                assert publication.status == "published"

                child = await _repeat_child(session, root_task_id=task_id)
                child_publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == int(child.id)
                        )
                    )
                ).scalar_one_or_none()
                assert child_publication is not None

                verification = await CanonicalRepeatReservationVerifier(session).verify(
                    publication_id
                )
                assert verification.outcome == "matched"
                assert verification.successor_legacy_post_task_id == int(child.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_disabled_shadow_keeps_legacy_repeat_scheduling_without_reservation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-shadow-disabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed_repeat(Session, seed=2)

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_shadow_planning=False,
                )
                await scheduler._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )

                child = await _repeat_child(session, root_task_id=task_id)
                assert child is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                assert CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY not in dict(
                    publication.meta or {}
                )
                assert CANONICAL_REPEAT_PLAN_RESERVATION_META_KEY not in dict(
                    schedule.meta or {}
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_shadow_reservation_failure_never_blocks_legacy_successor(monkeypatch, tmp_path) -> None:
    class FailingReservationService:
        def __init__(self, _session) -> None:
            pass

        async def reserve_next(self, publication_id: int, *, after=None):
            raise RuntimeError("shadow-only failure")

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-shadow-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, _ = await _seed_repeat(Session, seed=3)
            monkeypatch.setattr(
                scheduler_module,
                "CanonicalRepeatPlanReservationService",
                FailingReservationService,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_shadow_planning=True,
                )
                await scheduler._schedule_next_repeat_if_needed(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )
                child = await _repeat_child(session, root_task_id=task_id)
                assert child is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
