from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_repeat_delivery_executor import (
    CanonicalPublicationRepeatDeliveryExecutor,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


async def _seed(
    Session,
    *,
    seed: int,
    repeat: bool,
    runtime_options: dict | None = None,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=208000 + seed,
            username=f"repeat-routing-{seed}",
            full_name=f"Repeat Routing {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100208000 + seed),
            title=f"Repeat Routing {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Repeat routing {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule=({"enabled": True, "seconds": 60} if repeat else None),
            runtime_options=runtime_options,
        )
        return int(publication.id)


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [6401]


def test_repeat_router_uses_repeat_atomic_path_only_when_continuation_is_available(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-router.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            disabled_id = await _seed(Session, seed=1, repeat=True, runtime_options={})
            disabled_sender = _Sender()
            disabled_delegate = CanonicalPublicationRepeatDeliveryExecutor(
                Session,
                sender=disabled_sender,
                allow_repeat=False,
                heartbeat_interval_seconds=120,
            )
            disabled_router = CanonicalPublicationRepeatHandoffExecutor(
                executor=disabled_delegate,
                session_factory=Session,
            )
            disabled = await disabled_router.execute(disabled_id)
            assert disabled.outcome == "ineligible"
            assert disabled_sender.calls == 0
            async with Session() as session:
                publication = await session.get(Publication, disabled_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id is not None
                task = await session.get(PostTask, int(publication.legacy_post_task_id))
                assert task is not None and task.status == "pending"

            enabled_id = await _seed(
                Session,
                seed=2,
                repeat=True,
                runtime_options={"silent": True},
            )
            sender = _Sender()
            delegate = CanonicalPublicationRepeatDeliveryExecutor(
                Session,
                sender=sender,
                allow_repeat=True,
                heartbeat_interval_seconds=120,
            )
            router = CanonicalPublicationRepeatHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )
            result = await router.execute(enabled_id)
            assert result.outcome == "published"
            assert sender.calls == 1

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert tick.materialized == 1

            replay = await router.execute(enabled_id)
            assert replay.outcome == "ineligible"
            assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_router_keeps_effectful_repeat_outside_first_profile(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-router-effects.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(
                Session,
                seed=3,
                repeat=True,
                runtime_options={"pin_on": True},
            )
            sender = _Sender()
            delegate = CanonicalPublicationRepeatDeliveryExecutor(
                Session,
                sender=sender,
                allow_repeat=True,
                heartbeat_interval_seconds=120,
            )
            router = CanonicalPublicationRepeatHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )
            result = await router.execute(publication_id)
            assert result.outcome == "ineligible"
            assert sender.calls == 0

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id is not None
                task = await session.get(PostTask, int(publication.legacy_post_task_id))
                assert task is not None and task.status == "pending"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_nonrepeat_linked_row_still_delegates_to_existing_handoff_stack(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-router-nonrepeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(
                Session,
                seed=4,
                repeat=False,
                runtime_options={"silent": True},
            )
            sender = _Sender()
            delegate = CanonicalPublicationRepeatDeliveryExecutor(
                Session,
                sender=sender,
                allow_repeat=True,
                heartbeat_interval_seconds=120,
            )
            router = CanonicalPublicationRepeatHandoffExecutor(
                executor=delegate,
                session_factory=Session,
            )
            result = await router.execute(publication_id)
            assert result.outcome == "published"
            assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
