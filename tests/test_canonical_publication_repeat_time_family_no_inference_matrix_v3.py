from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_repeat_capability_claim import (
    CanonicalPublicationRepeatCapabilityClaimService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_retired(
    Session,
    *,
    seed: int,
    runtime_options: dict[str, object],
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=260000 + seed,
            username=f"repeat-time-matrix-{seed}",
            full_name=f"Repeat Time Matrix {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100260000 + seed),
            title=f"Repeat Time Matrix Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100360000 + seed),
            title=f"Repeat Time Matrix Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()

        options = dict(runtime_options)
        if options.get("forward_to") == ["target"]:
            options["forward_to"] = [int(target.id)]
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "repeat time authority matrix",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
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


async def _claim(Session, publication_id: int, **overrides: bool):
    facts = {
        "allow_time_autodelete": True,
        "allow_views_autodelete": False,
        "allow_repeat": True,
        "allow_repeat_time": True,
        "allow_repeat_time_pin": False,
        "allow_repeat_time_forward": False,
        "allow_repeat_time_pin_forward": False,
        "allow_repeat_views": False,
        "allow_repeat_views_pin": False,
        "allow_repeat_views_forward": False,
        "allow_repeat_views_pin_forward": False,
    }
    facts.update(overrides)
    async with Session() as session:
        return await CanonicalPublicationRepeatCapabilityClaimService(
            session
        ).claim_supported(
            publication_id=publication_id,
            holder="repeat-time-family-matrix",
            ttl_seconds=180,
            **facts,
        )


async def _assert_pristine(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert publication.telegram_message_ids in (None, [])
        assert publication.result_link is None
        assert await session.get(PublicationDeliveryLease, publication_id) is None
        attempts = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all()
        assert attempts == []


def test_exact_time_family_profiles_require_only_their_explicit_fact_chain(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-family-accepted.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            plain = await _seed_retired(
                Session,
                seed=1,
                runtime_options={"autodelete_seconds": 90},
            )
            assert await _claim(Session, plain) is not None

            pin = await _seed_retired(
                Session,
                seed=2,
                runtime_options={"autodelete_seconds": 90, "pin_on": True},
            )
            assert await _claim(
                Session,
                pin,
                allow_repeat_time_pin=True,
            ) is not None

            forward = await _seed_retired(
                Session,
                seed=3,
                runtime_options={
                    "autodelete_seconds": 90,
                    "forward_to": ["target"],
                },
            )
            assert await _claim(
                Session,
                forward,
                allow_repeat_time_forward=True,
            ) is not None

            combined = await _seed_retired(
                Session,
                seed=4,
                runtime_options={
                    "autodelete_seconds": 90,
                    "pin_on": True,
                    "forward_to": ["target"],
                },
            )
            assert await _claim(
                Session,
                combined,
                allow_repeat_time_pin=True,
                allow_repeat_time_forward=True,
                allow_repeat_time_pin_forward=True,
            ) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_time_family_facts_never_substitute_for_missing_required_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-family-rejected.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            cases = (
                (10, {"autodelete_seconds": 90}, {"allow_repeat_time": False}),
                (
                    11,
                    {"autodelete_seconds": 90, "pin_on": True},
                    {
                        "allow_repeat_time_pin": False,
                        "allow_repeat_time_forward": True,
                        "allow_repeat_time_pin_forward": True,
                    },
                ),
                (
                    12,
                    {"autodelete_seconds": 90, "forward_to": ["target"]},
                    {
                        "allow_repeat_time_pin": True,
                        "allow_repeat_time_forward": False,
                        "allow_repeat_time_pin_forward": True,
                    },
                ),
                (
                    13,
                    {
                        "autodelete_seconds": 90,
                        "pin_on": True,
                        "forward_to": ["target"],
                    },
                    {
                        "allow_repeat_time_pin": True,
                        "allow_repeat_time_forward": True,
                        "allow_repeat_time_pin_forward": False,
                    },
                ),
            )
            for seed, options, facts in cases:
                publication_id = await _seed_retired(
                    Session,
                    seed=seed,
                    runtime_options=options,
                )
                assert await _claim(Session, publication_id, **facts) is None
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_time_views_remains_closed_even_when_all_current_facts_are_true(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-views-stays-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            profiles = (
                {"autodelete_seconds": 90, "autodelete_views": 10},
                {
                    "autodelete_seconds": 90,
                    "autodelete_views": 10,
                    "pin_on": True,
                },
                {
                    "autodelete_seconds": 90,
                    "autodelete_views": 10,
                    "forward_to": ["target"],
                },
                {
                    "autodelete_seconds": 90,
                    "autodelete_views": 10,
                    "pin_on": True,
                    "forward_to": ["target"],
                },
            )
            for seed, options in enumerate(profiles, start=20):
                publication_id = await _seed_retired(
                    Session,
                    seed=seed,
                    runtime_options=options,
                )
                claim = await _claim(
                    Session,
                    publication_id,
                    allow_views_autodelete=True,
                    allow_repeat_time_pin=True,
                    allow_repeat_time_forward=True,
                    allow_repeat_time_pin_forward=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                    allow_repeat_views_forward=True,
                    allow_repeat_views_pin_forward=True,
                )
                assert claim is None
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_generated_effective_timer_never_becomes_queue_time_repeat_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-generated-matrix.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired(
                Session,
                seed=30,
                runtime_options={
                    "autodelete_seconds": 90,
                    "pin_on": True,
                    "forward_to": ["target"],
                },
            )
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                assert schedule is not None
                for row in (publication, schedule):
                    meta = dict(row.meta or {})
                    options = dict(meta.get("runtime_options") or {})
                    options["autodelete_effective_seconds"] = 90
                    meta["runtime_options"] = options
                    row.meta = meta
                await session.commit()

            assert await _claim(
                Session,
                publication_id,
                allow_repeat_time_pin=True,
                allow_repeat_time_forward=True,
                allow_repeat_time_pin_forward=True,
            ) is None
            await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
