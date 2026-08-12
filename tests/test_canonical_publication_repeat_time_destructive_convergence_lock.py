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


async def _seed(Session, *, seed: int, options: dict[str, object]) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=237000 + seed,
            username=f"repeat-time-lock-{seed}",
            full_name=f"Repeat Time Lock {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100237000 + seed),
            title=f"Repeat Time Lock {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat time lock"}]
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
        return int(publication.id), int(publication.legacy_post_task_id)


async def _assert_pristine(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
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
        assert publication.legacy_post_task_id == task_id
        assert publication.telegram_message_ids in (None, [])
        assert publication.result_link is None
        assert task is not None and task.status == "pending"
        assert lease is None
        assert attempts == []


def test_generic_time_executor_fact_cannot_authorize_repeat_time(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-time-hard-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                options={
                    "silent": True,
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="repeat-time-lock",
                    ttl_seconds=180,
                    allow_time_autodelete=True,
                    allow_repeat=True,
                    # Even unrelated already-proven views-family facts cannot weaken
                    # the explicit repeat+time convergence lock.
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                    allow_repeat_views_forward=True,
                    allow_repeat_views_pin_forward=True,
                )
                assert claim is None

            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_generated_effective_timer_key_is_also_hard_closed_for_repeat(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-effective-time-hard-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=2,
                options={"autodelete_seconds": 90},
            )

            # `autodelete_effective_seconds` is intentionally reserved by the bridge and
            # cannot be queued as caller intent. Model generated/drifted canonical state
            # directly so this regression reaches the strict repeat claim boundary.
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                assert schedule is not None
                publication_meta = dict(publication.meta or {})
                schedule_meta = dict(schedule.meta or {})
                generated_options = {"autodelete_effective_seconds": 90}
                publication_meta["runtime_options"] = dict(generated_options)
                schedule_meta["runtime_options"] = dict(generated_options)
                publication.meta = publication_meta
                schedule.meta = schedule_meta
                await session.commit()

            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="repeat-effective-time-lock",
                    ttl_seconds=180,
                    allow_time_autodelete=True,
                    allow_repeat=True,
                )
                assert claim is None

            await _assert_pristine(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
