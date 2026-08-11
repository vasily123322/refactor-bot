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
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_recovery_reservation import (
    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_recovery_scheduler import Scheduler
from app.workers.canonical_scheduler import Scheduler as CanonicalScheduler


def _settings(**overrides) -> Settings:
    return Settings(
        BOT_TOKEN="123456:test-token-placeholder",
        API_ID=123456,
        API_HASH="0123456789abcdef0123456789abcdef",
        _env_file=None,
        **overrides,
    )


async def _seed_queued_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=108000 + seed,
            username=f"repeat-overdue-shadow-runtime-{seed}",
            full_name=f"Repeat Overdue Shadow Runtime {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(108100 + seed),
            title=f"Repeat Overdue Shadow Runtime {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Overdue runtime shadow"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options={
                "autodelete_views": 100,
                "autodelete_report": True,
            },
        )
        return int(publication.id), int(publication.legacy_post_task_id or 0)


async def _children(session, *, root_task_id: int) -> list[PostTask]:
    return list(
        (
            await session.execute(
                select(PostTask)
                .where(
                    PostTask.id != int(root_task_id),
                    PostTask.payload["repeat_group_id"].as_integer()
                    == int(root_task_id),
                )
                .order_by(PostTask.id.asc())
            )
        ).scalars().all()
    )


def test_overdue_recovery_shadow_flag_is_default_off_with_explicit_alias() -> None:
    default = _settings()
    assert default.canonical_repeat_overdue_recovery_shadow_enabled is False

    enabled = _settings(CANONICAL_REPEAT_OVERDUE_RECOVERY_SHADOW_ENABLED=True)
    assert enabled.canonical_repeat_overdue_recovery_shadow_enabled is True


def test_enabled_runtime_shadow_observes_one_legacy_overdue_recovery(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-overdue-runtime-shadow.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_overdue_recovery_shadow=True,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001 - recovery boundary

                skipped = await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )

                assert skipped is True
                children = await _children(session, root_task_id=task_id)
                assert len(children) == 1
                assert children[0].dedupe_key is None

                source = await session.get(Publication, publication_id)
                assert source is not None
                assert source.status == "skipped"
                reservation = dict(source.meta or {}).get(
                    CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY
                )
                assert isinstance(reservation, dict)
                assert reservation["source_publication_id"] == publication_id
                assert reservation["scheduled_at"] == (
                    source_at + timedelta(hours=4)
                ).isoformat()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_disabled_runtime_shadow_preserves_direct_legacy_recovery_path(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-overdue-runtime-disabled.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            boot_time = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = Scheduler(
                    session,
                    object(),
                    repeat_overdue_recovery_shadow=False,
                )
                scheduler._boot_time = boot_time  # noqa: SLF001 - recovery boundary

                skipped = await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
                    session,
                    task,
                    dict(task.payload or {}),
                )

                assert skipped is True
                children = await _children(session, root_task_id=task_id)
                assert len(children) == 1
                assert children[0].dedupe_key is None

                source = await session.get(Publication, publication_id)
                assert source is not None
                assert CANONICAL_REPEAT_RECOVERY_RESERVATION_META_KEY not in dict(
                    source.meta or {}
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_wrapper_does_not_override_boot_group_cleanup() -> None:
    assert "_boot_cleanup_repeats" not in Scheduler.__dict__
    assert Scheduler._boot_cleanup_repeats is CanonicalScheduler._boot_cleanup_repeats
