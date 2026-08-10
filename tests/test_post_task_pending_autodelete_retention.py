from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.post_task_retention import PostTaskRetentionService
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_pending(
    Session,
    *,
    channel_id: int,
    now: datetime,
    mode: str,
    report: bool = False,
) -> tuple[int, int]:
    async with Session() as session:
        runtime_options: dict[str, object]
        if mode == "time":
            runtime_options = {"autodelete_seconds": 3600}
        elif mode == "views":
            runtime_options = {"autodelete_views": 100}
        else:
            raise AssertionError("unsupported test mode")
        if report:
            runtime_options["autodelete_report"] = True

        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Pending retention {channel_id}",
                    }
                ]
            ),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(days=120),
            runtime_options=runtime_options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        result_id = channel_id * 100 + 1
        result_link = f"https://t.me/c/{channel_id}/{result_id}"
        payload = {
            **dict(task.payload or {}),
            **runtime_options,
            "result_ids": [result_id],
            "result_link": result_link,
        }
        if mode == "time":
            due = now + timedelta(hours=1)
            payload.update(
                {
                    "autodelete_effective_seconds": 3600,
                    "autodelete_at": due.isoformat(),
                    "autodeleted": False,
                }
            )
        task.payload = payload
        task.status = "done"
        await session.commit()

        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == int(publication.id),
                    PublicationAttempt.attempt == int(publication.attempt_count),
                )
            )
        ).scalar_one()
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id or 0))
        assert schedule is not None
        attempt.finished_at = now - timedelta(days=120)
        publication.result_link = result_link
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": dict(runtime_options),
        }
        if mode == "time":
            publication.meta = {
                **dict(publication.meta or {}),
                AUTODELETE_RUNTIME_META_KEY: {
                    "effective_seconds": 3600,
                    "scheduled_at": (now + timedelta(hours=1)).isoformat(),
                    "deleted": False,
                },
            }
        else:
            await PublicationAutodeleteViewStateService(session).sync_intent(
                publication_id=int(publication.id),
                threshold=100,
                now=now,
            )
        await session.commit()
        return int(task.id), int(publication.id)


def test_pending_autodelete_retirement_remains_disabled_by_default(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pending-retention-default.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id = await _seed_pending(
                Session,
                channel_id=941,
                now=now,
                mode="time",
                report=True,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.deleted == 0
            assert tick.skipped_pending_autodelete == 1
            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_opt_in_retires_exact_pending_time_intent_without_losing_runtime(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pending-retention-time.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id = await _seed_pending(
                Session,
                channel_id=942,
                now=now,
                mode="time",
                report=True,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_pending_autodelete=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.eligible == 1
            assert tick.deleted == 1
            assert tick.failures == 0
            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
                assert publication.meta["runtime_options"] == {
                    "autodelete_seconds": 3600,
                    "autodelete_report": True,
                }
                runtime = publication.meta[AUTODELETE_RUNTIME_META_KEY]
                assert runtime["effective_seconds"] == 3600
                assert runtime["deleted"] is False
                assert runtime["scheduled_at"] == (now + timedelta(hours=1)).isoformat()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_opt_in_retires_exact_pending_views_report_with_indexed_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pending-retention-views.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id = await _seed_pending(
                Session,
                channel_id=943,
                now=now,
                mode="views",
                report=True,
            )

            async with Session() as session:
                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_pending_autodelete=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.eligible == 1
            assert tick.deleted == 1
            assert tick.failures == 0
            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None and state is not None
                assert publication.legacy_post_task_id is None
                assert publication.meta["runtime_options"] == {
                    "autodelete_views": 100,
                    "autodelete_report": True,
                }
                assert state.threshold == 100
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pending_views_retirement_fails_closed_without_exact_index(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pending-retention-views-mismatch.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
            task_id, publication_id = await _seed_pending(
                Session,
                channel_id=944,
                now=now,
                mode="views",
                report=True,
            )
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                state.threshold = 101
                await session.commit()

                tick = await PostTaskRetentionService(
                    session,
                    retention_days=90,
                    batch_size=10,
                    retire_successful=True,
                    retire_successful_pending_autodelete=True,
                ).run_once(now=now)

            assert tick.selected == 1
            assert tick.deleted == 0
            assert tick.skipped_pending_autodelete == 1
            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())
