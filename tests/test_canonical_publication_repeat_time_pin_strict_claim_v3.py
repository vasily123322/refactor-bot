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
            tg_user_id=244000 + seed,
            username=f"repeat-time-pin-claim-{seed}",
            full_name=f"Repeat Time Pin Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100244000 + seed),
            title=f"Repeat Time Pin Claim Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100344000 + seed),
            title=f"Repeat Time Pin Claim Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()

        runtime_options = dict(options)
        if runtime_options.get("forward_to") == ["target"]:
            runtime_options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": "repeat time pin claim"}
                ]
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
        assert schedule is not None
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        if task is not None:
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
    allow_repeat_time_pin: bool = True,
):
    async with Session() as session:
        return await CanonicalPublicationRepeatCapabilityClaimService(
            session
        ).claim_supported(
            publication_id=publication_id,
            holder="repeat-time-pin-strict-claim",
            ttl_seconds=180,
            allow_repeat=allow_repeat,
            allow_time_autodelete=allow_time_autodelete,
            allow_repeat_time=allow_repeat_time,
            allow_repeat_time_pin=allow_repeat_time_pin,
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


def test_repeat_time_pin_requires_all_independent_authority_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-strict-facts.db'}"
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
                    "pin_on": True,
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                },
            )
            claim = await _claim(Session, accepted)
            assert claim is not None
            assert claim.plan.runtime_options() == {
                "silent": True,
                "pin_on": True,
                "autodelete_seconds": 90,
                "autodelete_report": True,
            }

            cases = (
                (2, False, True, True, True),
                (3, True, False, True, True),
                (4, True, True, False, True),
                (5, True, True, True, False),
            )
            for (
                seed,
                allow_repeat,
                allow_time,
                allow_repeat_time,
                allow_repeat_time_pin,
            ) in cases:
                publication_id = await _seed_retired(
                    Session,
                    seed=seed,
                    options={"autodelete_seconds": 90, "pin_on": True},
                )
                assert (
                    await _claim(
                        Session,
                        publication_id,
                        allow_repeat=allow_repeat,
                        allow_time_autodelete=allow_time,
                        allow_repeat_time=allow_repeat_time,
                        allow_repeat_time_pin=allow_repeat_time_pin,
                    )
                    is None
                )
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_strict_claim_rejects_other_time_compositions(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-strict-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                {"autodelete_seconds": 90, "pin_on": False},
                {"autodelete_seconds": 90, "forward_to": ["target"]},
                {
                    "autodelete_seconds": 90,
                    "pin_on": True,
                    "forward_to": ["target"],
                },
                {"autodelete_seconds": 90, "pin_on": True, "autodelete_views": 10},
            )
            for seed, options in enumerate(cases, start=10):
                publication_id = await _seed_retired(
                    Session,
                    seed=seed,
                    options=options,
                )
                assert await _claim(Session, publication_id) is None
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_time_pin_never_accepts_generated_effective_timer_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-pin-generated.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired(
                Session,
                seed=20,
                options={"autodelete_seconds": 90, "pin_on": True},
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
