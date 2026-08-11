from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import AdminConfig, Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_admin_log_planner import (
    CanonicalPublicationAdminLogPlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_published(
    Session,
    *,
    seed: int,
    with_log_chat: bool = True,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=132000 + seed,
            username=f"admin_log_author_{seed}",
            full_name=f"Admin Log Author {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(132100 + seed),
            title=f"Admin Log Channel {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        if with_log_chat:
            session.add(AdminConfig(log_chat_id=-(132900 + seed)))
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Canonical admin log proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            runtime_options={},
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
        publication.telegram_message_ids = [2201, 2202]
        publication.result_link = "https://t.me/c/12345/2202"
        publication.last_error = None
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[2201, 2202],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=timezone.utc),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_configured_admin_log_plan_uses_canonical_author_and_delivery_evidence(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-admin-log.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(Session, seed=1)

            async with Session() as session:
                plan = await CanonicalPublicationAdminLogPlanner(session).plan(publication_id)
                assert plan is not None
                assert plan.publication_id == publication_id
                assert plan.log_chat_id == -132901
                assert plan.source_telegram_chat_id == -132101
                assert plan.primary_message_id == 2202
                assert plan.result_link == "https://t.me/c/12345/2202"
                assert plan.author_tg_user_id == 132001
                assert plan.author_username == "admin_log_author_1"
                assert plan.author_full_name == "Admin Log Author 1"
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unconfigured_admin_log_has_no_action_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-admin-log-none.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(
                Session,
                seed=2,
                with_log_chat=False,
            )

            async with Session() as session:
                assert await CanonicalPublicationAdminLogPlanner(session).plan(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_delivery_evidence_drift_blocks_admin_log_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-admin-log-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(Session, seed=3)

            async with Session() as session:
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                attempt.telegram_message_ids = [9999]
                await session.commit()
                assert await CanonicalPublicationAdminLogPlanner(session).plan(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unsafe_result_link_is_not_exposed_to_admin_log(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-admin-log-link.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(Session, seed=4)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.result_link = "https://evil.example/SUPERSECRET"
                await session.commit()
                assert await CanonicalPublicationAdminLogPlanner(session).plan(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
