from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_time_forward_lifecycle_authority import (
    CanonicalRepeatTimeForwardLifecycleAuthorityService,
)
from app.services.canonical_repeat_time_pin_forward_lifecycle_authority import (
    CanonicalRepeatTimePinForwardLifecycleAuthorityService,
)
from app.services.canonical_repeat_time_pin_lifecycle_authority import (
    CanonicalRepeatTimePinLifecycleAuthorityService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal(Session, seed: int) -> tuple[int, tuple[int, int]]:
    now = datetime.now(timezone.utc)
    async with Session() as session:
        owner = Client(
            tg_user_id=255000 + seed,
            username=f"combined-life-{seed}",
            full_name="Combined Lifecycle",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100255000 + seed), title="Source", owner_id=int(owner.id), is_active=True
        )
        target_a = Channel(
            tg_chat_id=-(100355000 + seed), title="A", owner_id=int(owner.id), is_active=True
        )
        target_b = Channel(
            tg_chat_id=-(100455000 + seed), title="B", owner_id=int(owner.id), is_active=True
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        targets = (int(target_a.id), int(target_b.id))
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "combined lifecycle"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "forward_to": list(targets),
                "autodelete_seconds": 90,
            },
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        finished_at = now - timedelta(seconds=120)
        meta = dict(publication.meta or {})
        meta[AUTODELETE_RUNTIME_META_KEY] = {
            "deleted": False,
            "effective_seconds": 90,
            "scheduled_at": (finished_at + timedelta(seconds=90)).isoformat(),
        }
        publication.meta = meta
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = [9900 + seed]
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=[9900 + seed],
                error=None,
                meta={"canonical_delivery": True},
                finished_at=finished_at,
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id), targets


def test_combined_lifecycle_is_default_off_and_not_inferred_from_narrower_proofs(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-lifecycle.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, targets = await _seed_terminal(Session, 1)

            async with Session() as session:
                service = CanonicalRepeatTimePinForwardLifecycleAuthorityService(session)
                assert await service.lock_and_prove(publication_id) is None
                proof = await service.lock_and_prove(
                    publication_id,
                    allow_time_pin_forward=True,
                )
                assert proof is not None
                assert proof.pin_on is True
                assert proof.forward_channel_ids == targets
                assert proof.time_autodelete_seconds == 90

            async with Session() as session:
                assert await CanonicalRepeatTimePinLifecycleAuthorityService(
                    session
                ).lock_and_prove(publication_id, allow_time_pin=True) is None
            async with Session() as session:
                assert await CanonicalRepeatTimeForwardLifecycleAuthorityService(
                    session
                ).lock_and_prove(publication_id, allow_time_forward=True) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
