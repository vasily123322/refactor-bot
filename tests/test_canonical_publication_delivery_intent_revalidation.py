from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_publication(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=130000 + seed,
            username=f"canonical-intent-{seed}",
            full_name=f"Canonical Intent {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(130100 + seed),
            title=f"Canonical Intent {seed}",
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
                        "text": "Canonical intent revalidation",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        publication.legacy_post_task_id = None
        if task is not None:
            await session.delete(task)
        await session.commit()
        return int(publication.id), int(channel.id)


class _IntentDriftSender:
    def __init__(
        self,
        Session,
        publication_id: int,
        *,
        drift: Literal["runtime", "repeat", "channel"],
    ) -> None:
        self.Session = Session
        self.publication_id = publication_id
        self.drift = drift
        self.calls = 0

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        self.calls += 1
        async with self.Session() as session:
            publication = await session.get(Publication, self.publication_id)
            assert publication is not None
            schedule = await session.get(
                ScheduleEntry,
                int(publication.schedule_entry_id or 0),
            )
            assert schedule is not None
            if self.drift == "runtime":
                changed = {"runtime_options": {"silent": True}}
                publication.meta = dict(changed)
                schedule.meta = dict(changed)
            elif self.drift == "repeat":
                schedule.repeat_rule = {"enabled": True, "interval_seconds": 3600}
            else:
                channel = await session.get(Channel, int(publication.channel_id))
                assert channel is not None
                channel.tg_chat_id = int(channel.tg_chat_id) - 1000000
            await session.commit()
        return [1701]


async def _assert_ambiguous(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        assert publication.status == "sending"
        assert publication.telegram_message_ids is None
        assert publication.result_link is None
        assert publication.last_error is None
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert schedule is not None
        assert schedule.status == "pending"
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                )
            )
        ).scalar_one()
        assert attempt.status == "sending"
        assert attempt.telegram_message_ids is None
        assert attempt.finished_at is None
        assert attempt.error is None
        assert await session.get(PublicationDeliveryLease, publication_id) is not None


def test_runtime_drift_after_provider_side_effect_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-intent-runtime.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id = await _seed_publication(Session, seed=1)
            sender = _IntentDriftSender(
                Session,
                publication_id,
                drift="runtime",
            )
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                heartbeat_interval_seconds=120,
            ).execute(publication_id)
            assert result.outcome == "lease_lost"
            assert result.message_ids == ()
            assert sender.calls == 1
            await _assert_ambiguous(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_drift_after_provider_side_effect_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-intent-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id = await _seed_publication(Session, seed=2)
            sender = _IntentDriftSender(
                Session,
                publication_id,
                drift="repeat",
            )
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                heartbeat_interval_seconds=120,
            ).execute(publication_id)
            assert result.outcome == "lease_lost"
            assert sender.calls == 1
            await _assert_ambiguous(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_channel_destination_drift_after_provider_side_effect_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-intent-channel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id = await _seed_publication(Session, seed=3)
            sender = _IntentDriftSender(
                Session,
                publication_id,
                drift="channel",
            )
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                heartbeat_interval_seconds=120,
            ).execute(publication_id)
            assert result.outcome == "lease_lost"
            assert sender.calls == 1
            await _assert_ambiguous(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
