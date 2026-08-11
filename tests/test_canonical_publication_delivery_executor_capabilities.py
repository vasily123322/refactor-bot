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
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


class _TrackingSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        self.calls += 1
        return [1501]


async def _seed(
    Session,
    *,
    seed: int,
    runtime_options: dict,
    repeat_rule: dict | None = None,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=125000 + seed,
            username=f"canonical-executor-cap-{seed}",
            full_name=f"Canonical Executor Capability {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(125100 + seed),
            title=f"Canonical Executor Capability {seed}",
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
                        "text": "Capability-gated canonical delivery",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=runtime_options,
            repeat_rule=repeat_rule,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _assert_unclaimed(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        assert publication.status == "queued"
        assert publication.attempt_count == 0
        assert await session.get(PublicationDeliveryLease, publication_id) is None
        attempts = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all()
        assert attempts == []


def test_executor_rejects_runtime_options_before_claim_or_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'executor-cap-runtime.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(
                Session,
                seed=1,
                runtime_options={"silent": True},
            )
            sender = _TrackingSender()

            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
            ).execute(publication_id)

            assert result.outcome == "ineligible"
            assert sender.calls == 0
            await _assert_unclaimed(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_executor_rejects_repeat_schedule_before_claim_or_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'executor-cap-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(
                Session,
                seed=2,
                runtime_options={},
                repeat_rule={"enabled": True, "seconds": 3600},
            )
            sender = _TrackingSender()

            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
            ).execute(publication_id)

            assert result.outcome == "ineligible"
            assert sender.calls == 0
            await _assert_unclaimed(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
