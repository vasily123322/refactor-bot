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
    options: dict[str, object],
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=250000 + seed,
            username=f"repeat-time-forward-claim-{seed}",
            full_name=f"Repeat Time Forward Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100250000 + seed),
            title=f"Repeat Time Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100350000 + seed),
            title=f"Repeat Time Forward A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100450000 + seed),
            title=f"Repeat Time Forward B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()

        runtime_options = dict(options)
        if runtime_options.get("forward_to") == ["targets"]:
            runtime_options["forward_to"] = [int(target_a.id), int(target_b.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time forward claim"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=runtime_options,
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


async def _claim(
    Session,
    publication_id: int,
    *,
    allow_repeat: bool = True,
    allow_time_autodelete: bool = True,
    allow_repeat_time: bool = True,
    allow_repeat_time_forward: bool = True,
):
    async with Session() as session:
        return await CanonicalPublicationRepeatCapabilityClaimService(
            session
        ).claim_supported(
            publication_id=publication_id,
            holder="repeat-time-forward-strict-claim",
            ttl_seconds=180,
            allow_repeat=allow_repeat,
            allow_time_autodelete=allow_time_autodelete,
            allow_repeat_time=allow_repeat_time,
            allow_repeat_time_forward=allow_repeat_time_forward,
        )


async def _assert_pristine(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        lease = await session.get(PublicationDeliveryLease, publication_id)
        attempts = list(
            (
                await session.execute(
                    select(PublicationAttempt).where(
                        PublicationAttempt.publication_id == publication_id
                    )
                )
            ).scalars().all()
        )
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert publication.legacy_post_task_id is None
        assert publication.telegram_message_ids in (None, [])
        assert publication.result_link is None
        assert lease is None
        assert attempts == []


def test_repeat_time_forward_requires_all_independent_authority_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-strict-facts.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            accepted = await _seed_retired(
                Session,
                seed=1,
                options={
                    "silent": True,
                    "forward_to": ["targets"],
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                },
            )
            claim = await _claim(Session, accepted)
            assert claim is not None
            options = claim.plan.runtime_options()
            assert options["silent"] is True
            assert options["autodelete_seconds"] == 90
            assert options["autodelete_report"] is True
            assert len(options["forward_to"]) == 2

            cases = (
                (2, False, True, True, True),
                (3, True, False, True, True),
                (4, True, True, False, True),
                (5, True, True, True, False),
            )
            for seed, allow_repeat, allow_time, allow_repeat_time, allow_time_forward in cases:
                publication_id = await _seed_retired(
                    Session,
                    seed=seed,
                    options={"autodelete_seconds": 90, "forward_to": ["targets"]},
                )
                assert (
                    await _claim(
                        Session,
                        publication_id,
                        allow_repeat=allow_repeat,
                        allow_time_autodelete=allow_time,
                        allow_repeat_time=allow_repeat_time,
                        allow_repeat_time_forward=allow_time_forward,
                    )
                    is None
                )
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_forward_strict_claim_rejects_combined_views_and_empty_targets(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-strict-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                {"autodelete_seconds": 90, "forward_to": []},
                {
                    "autodelete_seconds": 90,
                    "pin_on": True,
                    "forward_to": ["targets"],
                },
                {
                    "autodelete_seconds": 90,
                    "forward_to": ["targets"],
                    "autodelete_views": 10,
                },
            )
            for seed, options in enumerate(cases, start=10):
                publication_id = await _seed_retired(Session, seed=seed, options=options)
                assert await _claim(Session, publication_id) is None
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_forward_never_accepts_generated_effective_timer_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-forward-generated.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired(
                Session,
                seed=20,
                options={"autodelete_seconds": 90, "forward_to": ["targets"]},
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
                assert schedule is not None
                for row in (publication, schedule):
                    meta = dict(row.meta or {})
                    runtime_options = dict(meta.get("runtime_options") or {})
                    runtime_options["autodelete_effective_seconds"] = 90
                    meta["runtime_options"] = runtime_options
                    row.meta = meta
                await session.commit()

            assert await _claim(Session, publication_id) is None
            await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
