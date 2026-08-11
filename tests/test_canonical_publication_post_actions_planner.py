from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_post_actions_planner import (
    CanonicalPublicationPostDeliveryActionPlanner,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_published(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> tuple[int, int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=130000 + seed,
            username=f"canonical-post-actions-{seed}",
            full_name=f"Canonical Post Actions {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(130100 + seed),
            title=f"Canonical Post Actions Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_one = Channel(
            tg_chat_id=-(130200 + seed),
            title=f"Canonical Post Actions Target One {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_two = Channel(
            tg_chat_id=-(130300 + seed),
            title=f"Canonical Post Actions Target Two {seed}",
            owner_id=int(owner.id),
            is_active=False,
        )
        session.add_all([source, target_one, target_two])
        await session.commit()

        options = deepcopy(runtime_options)
        if options.pop("_use_seed_targets", False):
            options["forward_to"] = [int(target_two.id), int(target_one.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Canonical post-delivery action proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
            runtime_options=options,
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
        publication.telegram_message_ids = [1901, 1902]
        publication.result_link = "https://t.me/c/12345/1902"
        publication.last_error = None
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[1901, 1902],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=timezone.utc),
            )
        )
        await session.delete(task)
        await session.commit()
        return (
            int(publication.id),
            int(source.tg_chat_id),
            int(target_one.tg_chat_id),
            int(target_two.tg_chat_id),
        )


def test_planner_maps_pin_and_ordered_forward_targets_without_post_task(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-post-actions.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, source_chat, target_one_chat, target_two_chat = (
                await _seed_published(
                    Session,
                    seed=1,
                    runtime_options={
                        "pin_on": True,
                        "silent": True,
                        "_use_seed_targets": True,
                    },
                )
            )

            async with Session() as session:
                plan = await CanonicalPublicationPostDeliveryActionPlanner(session).plan(
                    publication_id
                )
                assert plan is not None
                assert plan.publication_id == publication_id
                assert plan.source_telegram_chat_id == source_chat
                assert plan.message_ids == (1901, 1902)
                assert plan.pin_last_message is True
                assert plan.forward_silent is True
                assert [item.telegram_chat_id for item in plan.forward_targets] == [
                    target_two_chat,
                    target_one_chat,
                ]
                assert (await session.execute(select(PostTask.id))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_empty_secondary_intent_produces_empty_best_effort_action_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-post-actions-empty.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _source_chat, _target_one, _target_two = await _seed_published(
                Session,
                seed=2,
                runtime_options={},
            )

            async with Session() as session:
                plan = await CanonicalPublicationPostDeliveryActionPlanner(session).plan(
                    publication_id
                )
                assert plan is not None
                assert plan.pin_last_message is False
                assert plan.forward_targets == ()
                assert plan.forward_silent is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_runtime_intent_drift_between_publication_and_schedule_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-post-actions-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _source_chat, _target_one, _target_two = await _seed_published(
                Session,
                seed=3,
                runtime_options={"pin_on": True},
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": {"pin_on": False},
                }
                await session.commit()
                assert await CanonicalPublicationPostDeliveryActionPlanner(session).plan(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_malformed_duplicate_or_missing_forward_target_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-post-actions-invalid.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _source_chat, _target_one, _target_two = await _seed_published(
                Session,
                seed=4,
                runtime_options={"forward_to": [999999]},
            )

            async with Session() as session:
                planner = CanonicalPublicationPostDeliveryActionPlanner(session)
                assert await planner.plan(publication_id) is None

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                bad = {"forward_to": [1, 1]}
                publication.meta = {
                    **dict(publication.meta or {}),
                    "runtime_options": deepcopy(bad),
                }
                schedule.meta = {
                    **dict(schedule.meta or {}),
                    "runtime_options": deepcopy(bad),
                }
                await session.commit()
                assert await planner.plan(publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retired_legacy_attempt_does_not_authorize_secondary_action_replay(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-post-actions-legacy-origin.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _source_chat, _target_one, _target_two = await _seed_published(
                Session,
                seed=5,
                runtime_options={"pin_on": True},
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                attempt.meta = {"legacy_post_task_id": 12345}
                await session.commit()

                assert await CanonicalPublicationPostDeliveryActionPlanner(session).plan(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
