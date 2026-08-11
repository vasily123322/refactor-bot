from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services import canonical_repeat_recovery_shadow as shadow_module
from app.services.canonical_repeat_recovery_shadow import (
    CanonicalRepeatRecoveryShadowCoordinator,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.publication_scheduler import Scheduler as PublicationScheduler


async def _seed_queued_repeat(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=107000 + seed,
            username=f"repeat-recovery-shadow-{seed}",
            full_name=f"Repeat Recovery Shadow {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(107100 + seed),
            title=f"Repeat Recovery Shadow {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Recovery shadow"}]
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


def test_shadow_observes_real_legacy_recovery_without_changing_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-shadow.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=1,
                scheduled_at=source_at,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = PublicationScheduler(session, object())
                scheduler._boot_time = after  # noqa: SLF001
                calls = 0

                async def legacy_recover() -> bool:
                    nonlocal calls
                    calls += 1
                    return await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
                        session,
                        task,
                        dict(task.payload or {}),
                    )

                result = await CanonicalRepeatRecoveryShadowCoordinator(session).run(
                    post=task,
                    after=after,
                    legacy_recover=legacy_recover,
                )

                assert calls == 1
                assert result.legacy_recovered is True
                assert result.source_publication_id == publication_id
                assert result.reservation_outcome == "reserved"
                assert result.verification_outcome == "matched"
                children = await _children(session, root_task_id=task_id)
                assert len(children) == 1
                assert children[0].dedupe_key is None
                source = await session.get(Publication, publication_id)
                assert source is not None and source.status == "skipped"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_shadow_reservation_failure_never_blocks_legacy_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    class FailingReservationService:
        def __init__(self, _session) -> None:
            pass

        async def reserve_recovery(self, publication_id: int, *, after=None):
            raise RuntimeError("shadow-only failure")

    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-recovery-shadow-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            source_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
            after = source_at + timedelta(hours=3, minutes=30)
            publication_id, task_id = await _seed_queued_repeat(
                Session,
                seed=2,
                scheduled_at=source_at,
            )
            monkeypatch.setattr(
                shadow_module,
                "CanonicalRepeatRecoveryReservationService",
                FailingReservationService,
            )

            async with Session() as session:
                task = await session.get(PostTask, task_id)
                assert task is not None
                scheduler = PublicationScheduler(session, object())
                scheduler._boot_time = after  # noqa: SLF001
                calls = 0

                async def legacy_recover() -> bool:
                    nonlocal calls
                    calls += 1
                    return await scheduler._skip_overdue_repeat_and_schedule_next(  # noqa: SLF001
                        session,
                        task,
                        dict(task.payload or {}),
                    )

                result = await CanonicalRepeatRecoveryShadowCoordinator(session).run(
                    post=task,
                    after=after,
                    legacy_recover=legacy_recover,
                )

                assert calls == 1
                assert result.legacy_recovered is True
                assert result.source_publication_id == publication_id
                assert result.reservation_outcome == "failed"
                assert result.verification_outcome == "ineligible"
                children = await _children(session, root_task_id=task_id)
                assert len(children) == 1
                assert children[0].dedupe_key is None
        finally:
            await engine.dispose()

    asyncio.run(run())
