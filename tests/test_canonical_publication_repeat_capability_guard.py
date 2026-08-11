from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_retired(
    Session,
    *,
    seed: int,
    repeat: bool,
    runtime_options: dict | None,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=209000 + seed,
            username=f"repeat-guard-{seed}",
            full_name=f"Repeat Guard {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100209000 + seed),
            title=f"Repeat Guard Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100309000 + seed),
            title=f"Repeat Guard Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()
        options = dict(runtime_options or {})
        if options.pop("__forward__", False):
            options["forward_to"] = [int(target.id)]
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Repeat guard {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule=({"enabled": True, "seconds": 60} if repeat else None),
            runtime_options=options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [6501]


def test_repeat_profile_accepts_plain_silent_and_pin_only(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-guard.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            for seed, options in (
                (1, {}),
                (2, {"silent": True}),
                (3, {"silent": False}),
                (4, {"pin_on": True}),
                (5, {"silent": True, "pin_on": True}),
                (6, {"pin_on": False}),
            ):
                publication_id = await _seed_retired(
                    Session,
                    seed=seed,
                    repeat=True,
                    runtime_options=options,
                )
                sender = _Sender()
                executor = CanonicalPublicationSafeRepeatDeliveryExecutor(
                    Session,
                    sender=sender,
                    allow_repeat=True,
                    heartbeat_interval_seconds=120,
                )
                result = await executor.execute(publication_id)
                assert result.outcome == "published"
                assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_forward_time_and_views_remain_fail_closed_even_when_dependencies_exist(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-guard-effects.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            cases = [
                (10, {"__forward__": True}),
                (11, {"autodelete_seconds": 60}),
                (12, {"autodelete_views": 5}),
                (13, {"pin_on": True, "__forward__": True}),
                (14, {"pin_on": True, "autodelete_seconds": 60}),
                (15, {"pin_on": True, "autodelete_views": 5}),
            ]
            for seed, options in cases:
                publication_id = await _seed_retired(
                    Session,
                    seed=seed,
                    repeat=True,
                    runtime_options=options,
                )
                sender = _Sender()
                executor = CanonicalPublicationSafeRepeatDeliveryExecutor(
                    Session,
                    sender=sender,
                    allow_repeat=True,
                    allow_time_autodelete=True,
                    allow_views_autodelete=True,
                    heartbeat_interval_seconds=120,
                )
                result = await executor.execute(publication_id)
                assert result.outcome == "ineligible"
                assert sender.calls == 0
                async with Session() as session:
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    assert publication.status == "queued"
                    assert int(publication.attempt_count or 0) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_nonrepeat_effectful_delivery_keeps_full_existing_capability_surface(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-guard-nonrepeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired(
                Session,
                seed=20,
                repeat=False,
                runtime_options={"pin_on": True},
            )
            sender = _Sender()
            executor = CanonicalPublicationSafeRepeatDeliveryExecutor(
                Session,
                sender=sender,
                allow_repeat=True,
                heartbeat_interval_seconds=120,
            )
            result = await executor.execute(publication_id)
            assert result.outcome == "published"
            assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
