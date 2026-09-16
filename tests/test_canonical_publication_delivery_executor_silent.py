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


async def _seed_retired(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=182000 + seed,
            username=f"silent-executor-{seed}",
            full_name=f"Silent Executor {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(182100 + seed),
            title=f"Silent Executor {seed}",
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
                        "text": f"Silent executor {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=runtime_options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        publication.legacy_post_task_id = None
        if task is not None:
            await session.delete(task)
        await session.commit()
        return int(publication.id), int(channel.id), int(channel.tg_chat_id)


class _SilentCaptureSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.next_id = 2100

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
        disable_notification: bool | None = None,
    ) -> list[int]:
        self.next_id += 1
        self.calls.append(
            {
                "chat_id": int(chat_id),
                "asset_channel_id": asset_channel_id,
                "disable_notification": disable_notification,
                "text": document.blocks[0]["text"],
            }
        )
        return [self.next_id]


class _NoCallSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        raise AssertionError("unsupported runtime must not reach provider sender")


def test_executor_forwards_explicit_silent_true_and_false(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-executor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            sender = _SilentCaptureSender()

            for seed, intended in ((1, True), (2, False)):
                publication_id, channel_id, tg_chat_id = await _seed_retired(
                    Session,
                    seed=seed,
                    runtime_options={"silent": intended},
                )
                result = await CanonicalPublicationDeliveryExecutor(
                    Session,
                    sender=sender,
                    heartbeat_interval_seconds=0.01,
                ).execute(publication_id)
                assert result.outcome == "published"
                call = sender.calls[-1]
                assert call["chat_id"] == tg_chat_id
                assert call["asset_channel_id"] == channel_id
                assert call["disable_notification"] is intended

                async with Session() as session:
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    assert publication.status == "published"
                    assert await session.get(
                        PublicationDeliveryLease, publication_id
                    ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_executor_unknown_runtime_option_is_ineligible_before_sender_or_claim_writes(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-executor-unsupported.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id, _tg_chat_id = await _seed_retired(
                Session,
                seed=3,
                runtime_options={"silent": True, "autodelete_seconds": 60},
            )
            sender = _NoCallSender()
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
            ).execute(publication_id)

            assert result.outcome == "ineligible"
            assert sender.calls == 0
            async with Session() as session:
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
                assert await session.get(
                    PublicationDeliveryLease, publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
