from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    runtime_options: dict,
    repeat_rule: dict | None = None,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=120000 + seed,
            username=f"canonical-runtime-{seed}",
            full_name=f"Canonical Runtime {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100120000 + seed),
            title=f"Canonical Runtime {seed}",
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
                        "text": "Canonical runtime proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options=runtime_options,
            repeat_rule=repeat_rule,
        )
        return int(publication.id), int(publication.legacy_post_task_id or 0)


async def _retire_and_claim(
    Session,
    *,
    publication_id: int,
    task_id: int,
    now: datetime,
):
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        assert publication is not None and task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        claim = await CanonicalPublicationDeliveryClaimService(session).claim(
            publication_id=publication_id,
            holder="runtime-test",
            now=now,
        )
        assert claim is not None
        return claim


def _runtime_due(meta: dict) -> datetime:
    runtime = dict(meta[AUTODELETE_RUNTIME_META_KEY])
    value = datetime.fromisoformat(str(runtime["scheduled_at"]))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def test_success_finalization_persists_time_autodelete_runtime_atomically(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-runtime-timer.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                scheduled_at=scheduled_at,
                runtime_options={"autodelete_seconds": 3600},
            )
            claim = await _retire_and_claim(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=scheduled_at + timedelta(minutes=1),
            )
            assert claim.plan.repeat_rule() == {}
            delivered_at = scheduled_at + timedelta(minutes=2)

            async with Session() as session:
                result = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[1001],
                    now=delivered_at,
                    finished_at=delivered_at,
                )
                assert result.outcome == "published"

                publication = await session.get(Publication, publication_id)
                assert publication is not None
                meta = dict(publication.meta or {})
                runtime = dict(meta[AUTODELETE_RUNTIME_META_KEY])
                assert runtime["deleted"] is False
                assert runtime["effective_seconds"] == 3600
                assert _runtime_due(meta) == delivered_at + timedelta(seconds=3600)
                assert meta["runtime_options"] == {"autodelete_seconds": 3600}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_aligned_autodelete_due_uses_canonical_schedule_anchor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-runtime-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=2,
                scheduled_at=scheduled_at,
                runtime_options={"autodelete_seconds": 3600},
                repeat_rule={"enabled": True, "seconds": 3600},
            )
            claim = await _retire_and_claim(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=scheduled_at + timedelta(minutes=1),
            )
            assert claim.plan.repeat_rule() == {"enabled": True, "seconds": 3600}
            delivered_at = scheduled_at + timedelta(minutes=2)

            async with Session() as session:
                result = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[1002],
                    now=delivered_at,
                    finished_at=delivered_at,
                )
                assert result.outcome == "published"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert _runtime_due(dict(publication.meta or {})) == (
                    scheduled_at + timedelta(seconds=3600)
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_intent_drift_after_claim_blocks_success_commit(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-runtime-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=3,
                scheduled_at=scheduled_at,
                runtime_options={"autodelete_seconds": 3600},
                repeat_rule={"enabled": True, "seconds": 3600},
            )
            claim = await _retire_and_claim(
                Session,
                publication_id=publication_id,
                task_id=task_id,
                now=scheduled_at + timedelta(minutes=1),
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.repeat_rule = {"enabled": True, "seconds": 7200}
                await session.commit()

            delivered_at = scheduled_at + timedelta(minutes=2)
            async with Session() as session:
                result = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[1003],
                    now=delivered_at,
                    finished_at=delivered_at,
                )
                assert result.outcome == "conflict"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert attempt.finished_at is None
                assert await CanonicalPublicationDeliveryClaimService(session).current(
                    publication_id
                ) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_generated_runtime_blocks_claim_before_delivery_side_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-runtime-stale.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=4,
                scheduled_at=scheduled_at,
                runtime_options={},
            )

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and task is not None
                publication.legacy_post_task_id = None
                publication.meta = {
                    **dict(publication.meta or {}),
                    AUTODELETE_RUNTIME_META_KEY: {
                        "deleted": False,
                        "effective_seconds": 60,
                        "scheduled_at": (
                            scheduled_at + timedelta(seconds=60)
                        ).isoformat(),
                    },
                }
                await session.delete(task)
                await session.commit()

                claim = await CanonicalPublicationDeliveryClaimService(session).claim(
                    publication_id=publication_id,
                    holder="stale-runtime-test",
                    now=scheduled_at + timedelta(minutes=1),
                )
                assert claim is None
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert attempts == []
                assert await CanonicalPublicationDeliveryClaimService(session).current(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
