from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.repositories.content import ContentRepo
from app.services import canonical_repeat_boot_recovery_shadow as shadow_module
from app.services.canonical_repeat_boot_recovery_shadow import (
    CanonicalRepeatBootRecoveryShadowCoordinator,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import cleanup_runtime_fields
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


_RUNTIME_OPTIONS = {
    "autodelete_views": 100,
    "autodelete_report": True,
}


async def _seed_group(
    Session,
    *,
    seed: int,
    source_at: datetime,
) -> tuple[tuple[int, int], tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=114000 + seed,
            username=f"repeat-boot-shadow-{seed}",
            full_name=f"Repeat Boot Shadow {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(114100 + seed),
            title=f"Repeat Boot Shadow {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Boot shadow"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        root_publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=source_at,
            repeat_rule={"enabled": True, "seconds": 3600},
            runtime_options=deepcopy(_RUNTIME_OPTIONS),
        )
        root_task_id = int(root_publication.legacy_post_task_id or 0)
        root_task = await session.get(PostTask, root_task_id)
        assert root_task is not None

        second_payload = cleanup_runtime_fields(dict(root_task.payload or {}))
        second_payload["repeat_on"] = True
        second_payload["repeat_seconds"] = 3600
        second_payload["repeat_group_id"] = root_task_id
        second_task = PostTask(
            channel_id=int(root_task.channel_id),
            status="pending",
            payload=second_payload,
            dedupe_key=None,
            scheduled_at=source_at + timedelta(hours=1),
        )
        session.add(second_task)
        await session.commit()
        await session.refresh(second_task)
        second_publication = await mirror_legacy_post_task(session, second_task)
        assert second_publication is not None
        return (
            (int(root_publication.id), int(second_publication.id)),
            (root_task_id, int(second_task.id)),
        )


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


def test_shadow_observes_full_legacy_boot_group_cleanup_once(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-shadow.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_ids, task_ids = await _seed_group(
                Session,
                seed=1,
                source_at=source_at,
            )

            async with Session() as session:
                tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
                assert all(task is not None for task in tasks)
                selected = [task for task in tasks if task is not None]
                scheduler = PublicationScheduler(session, object())
                scheduler._boot_time = after  # noqa: SLF001
                calls = 0

                async def legacy_cleanup() -> list[PostTask]:
                    nonlocal calls
                    calls += 1
                    return await scheduler._boot_cleanup_repeats(  # noqa: SLF001
                        session,
                        selected,
                    )

                result = await CanonicalRepeatBootRecoveryShadowCoordinator(session).run(
                    items=selected,
                    after=after,
                    legacy_cleanup=legacy_cleanup,
                )

                assert calls == 1
                assert result.remaining == ()
                assert len(result.groups) == 1
                group = result.groups[0]
                assert group.repeat_group_id == task_ids[0]
                assert group.source_publication_ids == publication_ids
                assert group.reservation_outcome == "reserved"
                assert group.verification_outcome == "matched"
                children = await _children(session, root_task_id=task_ids[0])
                assert len(children) == 1
                assert children[0].dedupe_key is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_shadow_preserves_nonrepeat_remaining_from_authoritative_batch(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-shadow-remaining.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            _publication_ids, task_ids = await _seed_group(
                Session,
                seed=2,
                source_at=source_at,
            )

            async with Session() as session:
                repeat_tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
                assert all(task is not None for task in repeat_tasks)
                channel_id = int(repeat_tasks[0].channel_id)  # type: ignore[union-attr]
                nonrepeat = PostTask(
                    channel_id=channel_id,
                    status="pending",
                    payload={"type": "text", "text": "Non-repeat"},
                    dedupe_key=None,
                    scheduled_at=source_at,
                )
                session.add(nonrepeat)
                await session.commit()
                await session.refresh(nonrepeat)
                selected = [task for task in repeat_tasks if task is not None] + [nonrepeat]
                scheduler = PublicationScheduler(session, object())
                scheduler._boot_time = after  # noqa: SLF001

                async def legacy_cleanup() -> list[PostTask]:
                    return await scheduler._boot_cleanup_repeats(  # noqa: SLF001
                        session,
                        selected,
                    )

                result = await CanonicalRepeatBootRecoveryShadowCoordinator(session).run(
                    items=selected,
                    after=after,
                    legacy_cleanup=legacy_cleanup,
                )

                assert [int(post.id) for post in result.remaining] == [int(nonrepeat.id)]
                assert len(result.groups) == 1
                assert result.groups[0].verification_outcome == "matched"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_shadow_reservation_failure_never_blocks_legacy_group_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    class FailingReservationService:
        def __init__(self, _session) -> None:
            pass

        async def reserve_group(self, source_publication_ids, *, after=None):
            raise RuntimeError("shadow-only group failure")

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-boot-shadow-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            _publication_ids, task_ids = await _seed_group(
                Session,
                seed=3,
                source_at=source_at,
            )
            monkeypatch.setattr(
                shadow_module,
                "CanonicalRepeatBootRecoveryReservationService",
                FailingReservationService,
            )

            async with Session() as session:
                tasks = [await session.get(PostTask, task_id) for task_id in task_ids]
                assert all(task is not None for task in tasks)
                selected = [task for task in tasks if task is not None]
                scheduler = PublicationScheduler(session, object())
                scheduler._boot_time = after  # noqa: SLF001
                calls = 0

                async def legacy_cleanup() -> list[PostTask]:
                    nonlocal calls
                    calls += 1
                    return await scheduler._boot_cleanup_repeats(  # noqa: SLF001
                        session,
                        selected,
                    )

                result = await CanonicalRepeatBootRecoveryShadowCoordinator(session).run(
                    items=selected,
                    after=after,
                    legacy_cleanup=legacy_cleanup,
                )

                assert calls == 1
                assert result.remaining == ()
                assert len(result.groups) == 1
                assert result.groups[0].reservation_outcome == "failed"
                assert result.groups[0].verification_outcome is None
                children = await _children(session, root_task_id=task_ids[0])
                assert len(children) == 1
                assert children[0].dedupe_key is None
        finally:
            await engine.dispose()

    asyncio.run(run())
