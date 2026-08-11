from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_autodelete_runtime_planner import (
    CanonicalPublicationAutodeleteRuntimePlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_published(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    delivered_at: datetime,
    runtime_options: dict,
    repeat_rule: dict | None = None,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=122000 + seed,
            username=f"canonical-autodelete-plan-{seed}",
            full_name=f"Canonical Autodelete Plan {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(122100 + seed),
            title=f"Canonical Autodelete Plan {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Canonical autodelete runtime proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule=repeat_rule,
            runtime_options=deepcopy(runtime_options),
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert task is not None and schedule is not None

        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [801]
        publication.result_link = "https://t.me/c/12345/801"
        publication.last_error = None
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[801],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=delivered_at,
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_nonrepeat_time_based_runtime_uses_finished_attempt_after_transport_retirement(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-plan.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            delivered_at = scheduled_at + timedelta(minutes=3)
            publication_id = await _seed_published(
                Session,
                seed=1,
                scheduled_at=scheduled_at,
                delivered_at=delivered_at,
                runtime_options={
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                plan = await CanonicalPublicationAutodeleteRuntimePlanner(session).plan(
                    publication_id
                )
                assert plan is not None
                assert plan.publication_id == publication_id
                assert plan.effective_seconds == 90
                assert plan.scheduled_at == delivered_at + timedelta(seconds=90)
                assert plan.state == {
                    "deleted": False,
                    "effective_seconds": 90,
                    "scheduled_at": (delivered_at + timedelta(seconds=90)).isoformat(),
                }
                assert plan.existing is False
                assert await session.get(PostTask, 1) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_exact_existing_runtime_is_idempotent_and_runtime_drift_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-existing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            delivered_at = scheduled_at + timedelta(minutes=1)
            publication_id = await _seed_published(
                Session,
                seed=2,
                scheduled_at=scheduled_at,
                delivered_at=delivered_at,
                runtime_options={"autodelete_seconds": 60},
            )

            async with Session() as session:
                planner = CanonicalPublicationAutodeleteRuntimePlanner(session)
                first = await planner.plan(publication_id)
                assert first is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.meta = {
                    **dict(publication.meta or {}),
                    AUTODELETE_RUNTIME_META_KEY: deepcopy(first.state),
                }
                await session.commit()

                repeated = await planner.plan(publication_id)
                assert repeated is not None
                assert repeated.existing is True
                assert repeated.state == first.state

                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"autodelete_seconds": 120},
                }
                await session.commit()
                assert await planner.plan(publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_and_views_based_autodelete_remain_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-unsupported.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            delivered_at = scheduled_at + timedelta(minutes=1)
            repeat_id = await _seed_published(
                Session,
                seed=3,
                scheduled_at=scheduled_at,
                delivered_at=delivered_at,
                repeat_rule={"enabled": True, "seconds": 300},
                runtime_options={"autodelete_seconds": 300},
            )
            views_id = await _seed_published(
                Session,
                seed=4,
                scheduled_at=scheduled_at,
                delivered_at=delivered_at,
                runtime_options={
                    "autodelete_seconds": 60,
                    "autodelete_views": 100,
                },
            )

            async with Session() as session:
                planner = CanonicalPublicationAutodeleteRuntimePlanner(session)
                assert await planner.plan(repeat_id) is None
                assert await planner.plan(views_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
