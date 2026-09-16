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
from app.services.canonical_publication_autodelete_runtime_writer import (
    CanonicalPublicationAutodeleteRuntimeWriter,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_published(
    Session,
    *,
    seed: int,
    delivered_at: datetime,
    runtime_options: dict,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=123000 + seed,
            username=f"canonical-autodelete-write-{seed}",
            full_name=f"Canonical Autodelete Write {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(123100 + seed),
            title=f"Canonical Autodelete Write {seed}",
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
                        "text": "Canonical autodelete writer proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=delivered_at - timedelta(minutes=1),
            runtime_options=deepcopy(runtime_options),
        )
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert schedule is not None

        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [901]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[901],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=delivered_at,
            )
        )
        if task is not None:
            await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_materialize_creates_exact_runtime_once_without_transport(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-write.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            delivered_at = datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc)
            publication_id = await _seed_published(
                Session,
                seed=1,
                delivered_at=delivered_at,
                runtime_options={"autodelete_seconds": 120},
            )

            async with Session() as session:
                writer = CanonicalPublicationAutodeleteRuntimeWriter(session)
                created = await writer.materialize(publication_id)
                assert created.outcome == "created"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.legacy_post_task_id is None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY] == {
                    "deleted": False,
                    "effective_seconds": 120,
                    "scheduled_at": (
                        delivered_at + timedelta(seconds=120)
                    ).isoformat(),
                }

                repeated = await writer.materialize(publication_id)
                assert repeated.outcome == "existing"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unsupported_views_semantics_do_not_write_generated_runtime(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-write-views.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(
                Session,
                seed=2,
                delivered_at=datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc),
                runtime_options={
                    "autodelete_seconds": 60,
                    "autodelete_views": 10,
                },
            )

            async with Session() as session:
                result = await CanonicalPublicationAutodeleteRuntimeWriter(
                    session
                ).materialize(publication_id)
                assert result.outcome == "ineligible"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_conflicting_existing_runtime_is_never_repaired_or_overwritten(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-autodelete-write-conflict.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            delivered_at = datetime(2026, 8, 11, 12, 5, tzinfo=timezone.utc)
            publication_id = await _seed_published(
                Session,
                seed=3,
                delivered_at=delivered_at,
                runtime_options={"autodelete_seconds": 60},
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                conflicting = {
                    "deleted": False,
                    "effective_seconds": 60,
                    "scheduled_at": (
                        delivered_at + timedelta(seconds=999)
                    ).isoformat(),
                }
                publication.meta = {
                    **dict(publication.meta or {}),
                    AUTODELETE_RUNTIME_META_KEY: deepcopy(conflicting),
                }
                await session.commit()

                result = await CanonicalPublicationAutodeleteRuntimeWriter(
                    session
                ).materialize(publication_id)
                assert result.outcome == "ineligible"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY] == conflicting
        finally:
            await engine.dispose()

    asyncio.run(run())
