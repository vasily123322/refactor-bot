from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry
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
            tg_user_id=217000 + seed,
            username=f"repeat-views-claim-{seed}",
            full_name=f"Repeat Views Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100217000 + seed),
            title=f"Repeat Views Claim Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(100317000 + seed),
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
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert state is None


def test_repeat_views_never_composes_from_independent_repeat_and_views_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-three-factor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            missing_composition = await _seed_retired_repeat(
                Session,
                seed=1,
                runtime_options={"silent": True, "autodelete_views": 17},
            )
            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=missing_composition,
                    holder="missing-repeat-views-fact",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=False,
                )
                assert claim is None
            await _assert_pristine(Session, missing_composition)

            missing_views_executor = await _seed_retired_repeat(
                Session,
                seed=2,
                runtime_options={"autodelete_views": 18},
            )
            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=missing_views_executor,
                    holder="missing-views-executor",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=False,
                    allow_repeat_views=True,
                )
                assert claim is None
            await _assert_pristine(Session, missing_views_executor)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_exact_plain_slice_stages_occurrence_local_state_atomically(tmp_path) -> None:
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
                seed=3,
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

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert int(publication.attempt_count or 0) == 1
                assert state is not None
                assert int(state.threshold) == 23
                assert state.last_views is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_compositions_and_time_remain_closed_with_all_claim_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-strict-scope.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            cases = (
                (4, {"pin_on": True, "autodelete_views": 7}),
                (5, {"__forward__": True, "autodelete_views": 8}),
                (6, {"autodelete_seconds": 60}),
                (7, {"autodelete_seconds": 60, "autodelete_views": 9}),
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


def test_safe_repeat_executor_keeps_repeat_views_fact_default_off(tmp_path) -> None:
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
                seed=8,
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
                seed=9,
                runtime_options={"autodelete_views": 32},
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
