from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, ChannelSettings, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_owner_notice_planner import (
    CanonicalPublicationOwnerNoticePlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_published(
    Session,
    *,
    seed: int,
    repeat: bool = False,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=131000 + seed,
            username=f"notice_owner_{seed}",
            full_name=f"Notice Owner {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(131100 + seed),
            title=f"Notice Channel {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        session.add(
            ChannelSettings(
                channel_id=int(channel.id),
                autosign=None,
                split_rules=[],
                filters={"tz": "Europe/London"},
            )
        )
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Canonical owner notice proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            repeat_rule=(
                {"enabled": True, "seconds": 300}
                if repeat
                else {"enabled": False}
            ),
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
        publication.telegram_message_ids = [2101, 2102]
        publication.result_link = "https://t.me/c/12345/2102"
        publication.last_error = None
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[2101, 2102],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=timezone.utc),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_nonrepeat_owner_notice_is_planned_from_canonical_state_without_transport(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-owner-notice.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(Session, seed=1)

            async with Session() as session:
                plan = await CanonicalPublicationOwnerNoticePlanner(session).plan(
                    publication_id,
                    at=datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc),
                )
                assert plan is not None
                assert plan.publication_id == publication_id
                assert plan.owner_tg_user_id == 131001
                assert plan.owner_username == "notice_owner_1"
                assert plan.channel_title == "Notice Channel 1"
                assert plan.source_telegram_chat_id == -131101
                assert plan.result_link == "https://t.me/c/12345/2102"
                assert plan.delivered_count == 2
                assert plan.timezone_code == "Europe/London"
                assert plan.local_date_iso == "2026-08-11"
                assert plan.local_date_text == "11.08.2026"
                assert plan.local_time_text == "13:30"
                assert plan.callback_data == f"cp_open_pub:{publication_id}:2026-08-11"
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_publication_keeps_historical_notice_suppression(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-owner-notice-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_published(Session, seed=2, repeat=True)

            async with Session() as session:
                assert await CanonicalPublicationOwnerNoticePlanner(session).plan(
                    publication_id,
                    at=datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mismatched_attempt_delivery_evidence_blocks_notice(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-owner-notice-evidence.db'}"
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
                assert await CanonicalPublicationOwnerNoticePlanner(session).plan(
                    publication_id,
                    at=datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unsafe_result_link_blocks_notice_instead_of_echoing_untrusted_url(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-owner-notice-link.db'}"
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
                assert await CanonicalPublicationOwnerNoticePlanner(session).plan(
                    publication_id,
                    at=datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc),
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
