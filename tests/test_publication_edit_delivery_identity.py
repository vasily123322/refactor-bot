from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_edit_persistence import PublicationEditPersistenceService
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_published(Session) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=71301,
            username="delivery-owner",
            full_name="Delivery Owner",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-10071301,
            title="Delivery identity",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "Before"}]
            ),
            created_by_tg_user_id=71301,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        task.status = "done"
        task.payload = {
            **dict(task.payload or {}),
            "result_ids": [84101],
            "result_link": "https://t.me/c/71301/84101",
        }
        await session.commit()
        publication = await LegacyPublicationBridge(session).reconcile(int(publication.id))
        return int(item.id), int(publication.id), int(task.id)


def test_edit_fallback_enriches_latest_attempt_and_transport_delivery_ids(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-attempt-delivery.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, task_id = await _seed_published(Session)

            async with Session() as session:
                await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71301,
                    expected_revision=1,
                    payload={"type": "text", "text": "After fallback"},
                    telegram_message_ids=[84202],
                )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == int(publication.attempt_count),
                        )
                    )
                ).scalar_one()
                assert publication.telegram_message_ids == [84202]
                assert publication.result_link == "https://t.me/c/71301/84202"
                assert attempt.telegram_message_ids == [84202]
                assert dict(task.payload or {})["result_ids"] == [84202]
                assert dict(task.payload or {})["result_link"] == (
                    "https://t.me/c/71301/84202"
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_time_timer_creates_canonical_due_state_without_post_task(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'edit-unlinked-timer.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, publication_id, task_id = await _seed_published(Session)
            now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                await PublicationEditPersistenceService(session).persist_success(
                    publication_id=publication_id,
                    tg_user_id=71301,
                    expected_revision=1,
                    payload={
                        "type": "text",
                        "text": "Canonical timer",
                        "autodelete_seconds": 1800,
                    },
                    telegram_message_ids=[84101],
                    now=now,
                )

            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta["runtime_options"] == {
                    "autodelete_seconds": 1800
                }
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY] == {
                    "effective_seconds": 1800,
                    "scheduled_at": (now + timedelta(seconds=1800)).isoformat(),
                    "deleted": False,
                }
        finally:
            await engine.dispose()

    asyncio.run(run())
