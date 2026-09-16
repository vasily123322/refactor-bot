from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduler_errors import NO_MESSAGE_IDS_ERROR, SAFE_DELIVERY_ERROR


async def _seed_transport_retired_publication(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=124000 + seed,
            username=f"canonical-executor-{seed}",
            full_name=f"Canonical Executor {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(124100 + seed),
            title=f"Canonical Executor {seed}",
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
                        "text": "Canonical executor delivery",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options={},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        publication.legacy_post_task_id = None
        if task is not None:
            await session.delete(task)
        await session.commit()
        return int(publication.id), int(channel.id), int(channel.tg_chat_id)


class _SuccessSender:
    def __init__(self, ids: list[int]) -> None:
        self.ids = list(ids)
        self.calls: list[tuple[int, PostDocument, int | None]] = []

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        self.calls.append((int(chat_id), document.clone(), asset_channel_id))
        return list(self.ids)


class _FailingSender:
    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/sendMessage failed")


class _LeaseStealingSender:
    def __init__(self, Session, publication_id: int) -> None:
        self.Session = Session
        self.publication_id = publication_id

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        async with self.Session() as session:
            lease = await session.get(PublicationDeliveryLease, self.publication_id)
            assert lease is not None
            lease.lease_token = "recovery-owner-token"
            lease.holder = "recovery-owner"
            await session.commit()
        return [1301]


class _BlockingSender:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking sender must be cancelled")


class _HeartbeatInspectingSender:
    def __init__(self, Session, publication_id: int) -> None:
        self.Session = Session
        self.publication_id = publication_id
        self.renewed = False

    async def _expiry(self) -> datetime:
        async with self.Session() as session:
            lease = await session.get(PublicationDeliveryLease, self.publication_id)
            assert lease is not None
            return lease.expires_at

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        before = await self._expiry()
        await asyncio.sleep(0.05)
        after = await self._expiry()
        self.renewed = after > before
        return [1401]


def test_executor_publishes_transport_retired_canonical_document(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-executor-success.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            publication_id, channel_id, tg_chat_id = (
                await _seed_transport_retired_publication(
                    Session,
                    seed=1,
                    scheduled_at=scheduled_at,
                )
            )
            sender = _SuccessSender([1101, 1102])
            executor = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                heartbeat_interval_seconds=0.01,
            )
            result = await executor.execute(publication_id)
            assert result.outcome == "published"
            assert result.message_ids == (1101, 1102)
            assert len(sender.calls) == 1
            chat_id, document, asset_channel_id = sender.calls[0]
            assert chat_id == tg_chat_id
            assert asset_channel_id == channel_id
            assert document.blocks[0]["text"] == "Canonical executor delivery"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.telegram_message_ids == [1101, 1102]
                assert publication.legacy_post_task_id is None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None and schedule.status == "completed"
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                assert attempt.status == "published"
                assert attempt.telegram_message_ids == [1101, 1102]
                assert await CanonicalPublicationDeliveryClaimService(session).current(
                    publication_id
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_executor_provider_failure_persists_only_static_safe_error(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-executor-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id, _tg_chat_id = (
                await _seed_transport_retired_publication(
                    Session,
                    seed=2,
                    scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_FailingSender(),
            ).execute(publication_id)
            assert result.outcome == "failed"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "failed"
                assert publication.last_error == SAFE_DELIVERY_ERROR
                assert "SUPERSECRET" not in publication.last_error
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                assert attempt.error == SAFE_DELIVERY_ERROR
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_executor_empty_provider_ids_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-executor-empty.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id, _tg_chat_id = (
                await _seed_transport_retired_publication(
                    Session,
                    seed=3,
                    scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_SuccessSender([]),
            ).execute(publication_id)
            assert result.outcome == "failed"
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.last_error == NO_MESSAGE_IDS_ERROR
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_success_cannot_commit_after_exact_lease_token_is_stolen(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-executor-stolen.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id, _tg_chat_id = (
                await _seed_transport_retired_publication(
                    Session,
                    seed=4,
                    scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_LeaseStealingSender(Session, publication_id),
                heartbeat_interval_seconds=120,
            ).execute(publication_id)
            assert result.outcome == "lease_lost"
            assert result.message_ids == ()

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids is None
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert lease is not None
                assert lease.lease_token == "recovery-owner-token"
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert attempt.finished_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cancellation_preserves_sending_claim_for_fail_closed_expiry_recovery(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-executor-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id, _tg_chat_id = (
                await _seed_transport_retired_publication(
                    Session,
                    seed=5,
                    scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            sender = _BlockingSender()
            executor = CanonicalPublicationDeliveryExecutor(Session, sender=sender)
            execution = asyncio.create_task(executor.execute(publication_id))
            await sender.started.wait()
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_executor_heartbeats_typed_lease_during_slow_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'canonical-executor-heartbeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _channel_id, _tg_chat_id = (
                await _seed_transport_retired_publication(
                    Session,
                    seed=6,
                    scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            sender = _HeartbeatInspectingSender(Session, publication_id)
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                lease_seconds=30,
                heartbeat_interval_seconds=0.01,
            ).execute(publication_id)
            assert result.outcome == "published"
            assert sender.renewed is True
        finally:
            await engine.dispose()

    asyncio.run(run())
