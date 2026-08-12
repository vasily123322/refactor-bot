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
from app.services.canonical_publication_repeat_delivery_executor import (
    CanonicalPublicationRepeatDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed_retired_repeat(Session, *, seed: int, scheduled_at: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=205000 + seed,
            username=f"repeat-delivery-{seed}",
            full_name=f"Repeat Delivery {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100205000 + seed),
            title=f"Repeat Delivery {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Repeat delivery {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"silent": True},
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


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        assert kwargs.get("disable_notification") is True
        return [6201]


def test_repeat_delivery_is_default_off_and_explicit_gate_publishes_once(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-delivery-gate.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)

            disabled_id = await _seed_retired_repeat(
                Session,
                seed=1,
                scheduled_at=now - timedelta(minutes=1),
            )
            disabled_sender = _Sender()
            disabled = CanonicalPublicationRepeatDeliveryExecutor(
                Session,
                sender=disabled_sender,
                allow_repeat=False,
                heartbeat_interval_seconds=120,
            )
            result = await disabled.execute(disabled_id, now=now)
            assert result.outcome == "ineligible"
            assert disabled_sender.calls == 0
            async with Session() as session:
                publication = await session.get(Publication, disabled_id)
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert await session.get(PublicationDeliveryLease, disabled_id) is None

            enabled_id = await _seed_retired_repeat(
                Session,
                seed=2,
                scheduled_at=now - timedelta(minutes=1),
            )
            sender = _Sender()
            enabled = CanonicalPublicationRepeatDeliveryExecutor(
                Session,
                sender=sender,
                allow_repeat=True,
                heartbeat_interval_seconds=120,
            )
            published = await enabled.execute(enabled_id, now=now)
            assert published.outcome == "published"
            assert sender.calls == 1

            async with Session() as session:
                publication = await session.get(Publication, enabled_id)
                assert publication is not None
                assert publication.status == "published"
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == enabled_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "published"
                assert dict(attempt.meta or {}).get("canonical_delivery") is True

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            tick = await continuation.run_once(now=now + timedelta(seconds=2))
            assert tick.materialized == 1
            assert tick.conflicts == 0

            async with Session() as session:
                source = await session.get(Publication, enabled_id)
                assert source is not None
                successors = (
                    await session.execute(
                        select(Publication).where(
                            Publication.id != enabled_id,
                            Publication.content_item_id == int(source.content_item_id),
                            Publication.content_revision == int(source.content_revision),
                            Publication.channel_id == int(source.channel_id),
                        )
                    )
                ).scalars().all()
                assert len(successors) == 1
                successor = successors[0]
                assert successor.status == "queued"
                assert successor.legacy_post_task_id is not None
                successor_task = await session.get(
                    PostTask,
                    int(successor.legacy_post_task_id),
                )
                assert successor_task is not None
                assert successor_task.status == "pending"

            replay = await enabled.execute(enabled_id, now=now + timedelta(seconds=3))
            assert replay.outcome == "ineligible"
            assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
