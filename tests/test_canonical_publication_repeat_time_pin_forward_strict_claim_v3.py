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


async def _seed_retired(Session, seed: int) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=256000 + seed,
            username=f"combined-claim-{seed}",
            full_name="Combined Claim",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100256000 + seed), title="Source", owner_id=int(owner.id), is_active=True
        )
        a = Channel(
            tg_chat_id=-(100356000 + seed), title="A", owner_id=int(owner.id), is_active=True
        )
        b = Channel(
            tg_chat_id=-(100456000 + seed), title="B", owner_id=int(owner.id), is_active=True
        )
        session.add_all([source, a, b])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(blocks=[{"id": "b1", "type": "text", "text": "combined"}]),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "pin_on": True,
                "forward_to": [int(a.id), int(b.id)],
                "autodelete_seconds": 90,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        meta = dict(schedule.meta or {})
        meta.pop("legacy_post_task_id", None)
        schedule.meta = meta
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _claim(Session, publication_id: int, **overrides):
    facts = {
        "allow_repeat": True,
        "allow_time_autodelete": True,
        "allow_repeat_time": True,
        "allow_repeat_time_pin": True,
        "allow_repeat_time_forward": True,
        "allow_repeat_time_pin_forward": True,
    }
    facts.update(overrides)
    async with Session() as session:
        return await CanonicalPublicationRepeatCapabilityClaimService(
            session
        ).claim_supported(
            publication_id=publication_id,
            holder="combined-claim",
            ttl_seconds=180,
            **facts,
        )


async def _assert_pristine(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert await session.get(PublicationDeliveryLease, publication_id) is None
        assert (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all() == []


def test_combined_claim_requires_both_narrower_facts_plus_dedicated_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-claim.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            accepted = await _seed_retired(Session, 1)
            assert await _claim(Session, accepted) is not None

            missing = (
                {"allow_repeat_time_pin": False},
                {"allow_repeat_time_forward": False},
                {"allow_repeat_time_pin_forward": False},
                {"allow_repeat_time": False},
                {"allow_time_autodelete": False},
            )
            for index, override in enumerate(missing, start=2):
                publication_id = await _seed_retired(Session, index)
                assert await _claim(Session, publication_id, **override) is None
                await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
