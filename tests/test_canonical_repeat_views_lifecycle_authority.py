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
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import CanonicalRepeatPlanReservationService
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter
from app.services.canonical_repeat_views_lifecycle_authority import (
    CanonicalRepeatViewsLifecycleAuthorityService,
)
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import normalize_telegram_message_ids


async def _seed_terminal_repeat_views(
    Session,
    *,
    seed: int,
    now: datetime,
    runtime_options: dict | None = None,
) -> int:
    options = dict(
        runtime_options
        or {
            "silent": True,
            "autodelete_views": 17,
            "autodelete_report": True,
        }
    )
    async with Session() as session:
        owner = Client(
            tg_user_id=214000 + seed,
            username=f"repeat-views-lifecycle-{seed}",
            full_name=f"Repeat Views Lifecycle {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100214000 + seed),
            title=f"Repeat Views Lifecycle {seed}",
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
                        "text": f"repeat views lifecycle {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        threshold = int(options["autodelete_views"])
        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=threshold,
            now=now - timedelta(minutes=1),
        )

        message_ids = [8200 + seed, 8300 + seed]
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = list(message_ids)
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=list(message_ids),
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(seconds=30),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _prove(Session, publication_id: int):
    async with Session() as session:
        proof = await CanonicalRepeatViewsLifecycleAuthorityService(
            session
        ).lock_and_prove(publication_id)
        await session.rollback()
        return proof


def test_repeat_views_lifecycle_composes_locked_canonical_repeat_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-lifecycle.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id = await _seed_terminal_repeat_views(
                Session,
                seed=1,
                now=now,
            )

            proof = await _prove(Session, publication_id)
            assert proof is not None
            assert proof.publication_id == publication_id
            assert proof.attempt == 1
            assert proof.repeat_seconds == 60
            assert proof.threshold == 17
            assert proof.autodelete_report is True
            assert proof.telegram_message_ids == (8201, 8301)
            assert proof.runtime_options == {
                "silent": True,
                "autodelete_views": 17,
                "autodelete_report": True,
            }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_lifecycle_fails_closed_on_state_message_transport_or_profile_drift(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-lifecycle-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)

            threshold_drift = await _seed_terminal_repeat_views(
                Session,
                seed=2,
                now=now,
            )
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, threshold_drift)
                assert state is not None
                state.threshold = 18
                await session.commit()
            assert await _prove(Session, threshold_drift) is None

            message_drift = await _seed_terminal_repeat_views(Session, seed=3, now=now)
            async with Session() as session:
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == message_drift,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                attempt.telegram_message_ids = [9999]
                await session.commit()
            assert await _prove(Session, message_drift) is None

            relinked = await _seed_terminal_repeat_views(Session, seed=4, now=now)
            async with Session() as session:
                publication = await session.get(Publication, relinked)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                assert schedule is not None
                task = PostTask(
                    channel_id=int(publication.channel_id),
                    status="pending",
                    payload={
                        "repeat_on": True,
                        "repeat_seconds": 60,
                        "autodelete_views": 17,
                    },
                    dedupe_key=f"repeat-views-relink:{relinked}",
                    scheduled_at=schedule.scheduled_at,
                )
                session.add(task)
                await session.flush()
                publication.legacy_post_task_id = int(task.id)
                await session.commit()
            assert await _prove(Session, relinked) is None

            composed = await _seed_terminal_repeat_views(
                Session,
                seed=5,
                now=now,
                runtime_options={"pin_on": True, "autodelete_views": 17},
            )
            assert await _prove(Session, composed) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_successor_carries_only_intent_not_source_views_state(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-successor-isolation.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            source_id = await _seed_terminal_repeat_views(
                Session,
                seed=6,
                now=now,
                runtime_options={"silent": True, "autodelete_views": 23},
            )

            async with Session() as session:
                reserved = await CanonicalRepeatPlanReservationService(
                    session
                ).reserve_next(source_id, after=now)
                assert reserved.outcome == "reserved"

            async with Session() as session:
                materialized = await CanonicalRepeatTransportAdapter(session).materialize(
                    source_id
                )
                assert materialized.outcome == "created"
                assert materialized.publication_id is not None
                successor_id = int(materialized.publication_id)

            async with Session() as session:
                source = await session.get(Publication, source_id)
                successor = await session.get(Publication, successor_id)
                source_state = await session.get(PublicationAutodeleteViewState, source_id)
                successor_state = await session.get(
                    PublicationAutodeleteViewState,
                    successor_id,
                )
                assert source is not None and successor is not None
                assert source_state is not None
                assert int(source_state.threshold) == 23
                assert successor_state is None
                assert successor.status == "queued"
                assert normalize_telegram_message_ids(successor.telegram_message_ids) == []
                successor_meta = dict(successor.meta or {})
                assert successor_meta.get("runtime_options") == {
                    "silent": True,
                    "autodelete_views": 23,
                }
                assert AUTODELETE_RUNTIME_META_KEY not in successor_meta
                assert successor.legacy_post_task_id is not None

                successor_task = await session.get(
                    PostTask,
                    int(successor.legacy_post_task_id),
                )
                assert successor_task is not None
                payload = dict(successor_task.payload or {})
                assert payload.get("repeat_on") is True
                assert payload.get("repeat_seconds") == 60
                assert payload.get("autodelete_views") == 23
                assert payload.get("result_ids") in (None, [])
                assert payload.get("result_link") in (None, "")
                assert payload.get("autodelete_at") in (None, "")
                assert payload.get("autodelete_effective_seconds") in (
                    None,
                    False,
                    0,
                    "0",
                    "",
                )
                assert payload.get("autodeleted") in (None, False)

            # Successor creation only adds reservation metadata to the source. The same
            # occurrence-local views lifecycle authority remains intact for the source.
            proof_after_successor = await _prove(Session, source_id)
            assert proof_after_successor is not None
            assert proof_after_successor.threshold == 23
        finally:
            await engine.dispose()

    asyncio.run(run())
