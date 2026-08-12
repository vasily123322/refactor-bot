from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_repeat_capability_claim import (
    CanonicalPublicationRepeatCapabilityClaimService,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_retired_repeat(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=219000 + seed,
            username=f"repeat-views-claim-{seed}",
            full_name=f"Repeat Views Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100219000 + seed),
            title=f"Repeat Views Claim Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100319000 + seed),
            title=f"Repeat Views Claim Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()

        options = dict(runtime_options)
        if options.pop("__forward__", False):
            options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"repeat views claim {seed}",
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
        assert publication.legacy_post_task_id is not None
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _assert_pristine(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        state = await session.get(PublicationAutodeleteViewState, publication_id)
        lease = await session.get(PublicationDeliveryLease, publication_id)
        attempts = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all()
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert state is None
        assert lease is None
        assert attempts == []


def test_repeat_views_requires_repeat_views_executor_and_composition_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-three-factor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = (
                (
                    1,
                    {
                        "allow_repeat": True,
                        "allow_views_autodelete": True,
                        "allow_repeat_views": False,
                    },
                ),
                (
                    2,
                    {
                        "allow_repeat": True,
                        "allow_views_autodelete": False,
                        "allow_repeat_views": True,
                    },
                ),
                (
                    3,
                    {
                        "allow_repeat": False,
                        "allow_views_autodelete": True,
                        "allow_repeat_views": True,
                    },
                ),
            )
            for seed, flags in cases:
                publication_id = await _seed_retired_repeat(
                    Session,
                    seed=seed,
                    runtime_options={"silent": True, "autodelete_views": 17 + seed},
                )
                async with Session() as session:
                    claim = await CanonicalPublicationRepeatCapabilityClaimService(
                        session
                    ).claim_supported(
                        publication_id=publication_id,
                        holder=f"repeat-views-three-factor-{seed}",
                        ttl_seconds=180,
                        **flags,
                    )
                    assert claim is None
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_exact_plain_slice_stages_state_with_primary_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-exact-claim.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired_repeat(
                Session,
                seed=10,
                runtime_options={
                    "silent": True,
                    "autodelete_views": 23,
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="repeat-views-exact",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                )
                assert claim is not None
                assert claim.plan.publication_id == publication_id
                assert claim.plan.runtime_options() == {
                    "silent": True,
                    "autodelete_views": 23,
                    "autodelete_report": True,
                }

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                lease = await session.get(PublicationDeliveryLease, publication_id)
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert publication is not None
                assert publication.status == "sending"
                assert int(publication.attempt_count or 0) == 1
                assert state is not None
                assert int(state.threshold) == 23
                assert state.last_views is None
                assert lease is not None
                assert attempt.status == "sending"
                assert dict(attempt.meta or {}).get("canonical_delivery") is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_rejects_pin_forward_time_dual_and_unknown_compositions(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-strict-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            cases = (
                (20, {"pin_on": True, "autodelete_views": 7}),
                (21, {"pin_on": False, "autodelete_views": 7}),
                (22, {"__forward__": True, "autodelete_views": 8}),
                (23, {"forward_to": [], "autodelete_views": 8}),
                (24, {"autodelete_seconds": 60}),
                (25, {"autodelete_seconds": 60, "autodelete_views": 9}),
                (26, {"autodelete_views": 9, "future_effect": True}),
            )
            for seed, options in cases:
                publication_id = await _seed_retired_repeat(
                    Session,
                    seed=seed,
                    runtime_options=options,
                )
                async with Session() as session:
                    claim = await CanonicalPublicationRepeatCapabilityClaimService(
                        session
                    ).claim_supported(
                        publication_id=publication_id,
                        holder=f"repeat-views-scope-{seed}",
                        ttl_seconds=180,
                        allow_repeat=True,
                        allow_time_autodelete=True,
                        allow_views_autodelete=True,
                        allow_repeat_views=True,
                    )
                    assert claim is None
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_repeat_pin_forward_profile_does_not_need_repeat_views_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-established-profile.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired_repeat(
                Session,
                seed=30,
                runtime_options={"pin_on": True, "__forward__": True},
            )
            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="repeat-established-profile",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_repeat_views=False,
                )
                assert claim is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_safe_repeat_executor_has_independent_default_off_repeat_views_seam(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-executor-fact.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            publication_id = await _seed_retired_repeat(
                Session,
                seed=40,
                runtime_options={"autodelete_views": 31},
            )
            executor = CanonicalPublicationSafeRepeatDeliveryExecutor(
                Session,
                sender=object(),
                allow_repeat=True,
                allow_views_autodelete=True,
                heartbeat_interval_seconds=120,
            )
            assert executor.allow_repeat_views is False
            assert await executor._claim(publication_id, now=None) is None
            await _assert_pristine(Session, publication_id)

            explicit_id = await _seed_retired_repeat(
                Session,
                seed=41,
                runtime_options={"silent": False, "autodelete_views": 32},
            )
            explicit = CanonicalPublicationSafeRepeatDeliveryExecutor(
                Session,
                sender=object(),
                allow_repeat=True,
                allow_views_autodelete=True,
                allow_repeat_views=True,
                heartbeat_interval_seconds=120,
            )
            assert explicit.allow_repeat_views is True
            assert await explicit._claim(explicit_id, now=None) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
