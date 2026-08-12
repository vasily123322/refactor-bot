from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.publication_bridge import LegacyPublicationBridge


class _Sender:
    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        return [1801]


class _StaticResolver:
    async def resolve(self, *, chat_id: int, message_ids):
        return "https://t.me/c/999/1801"


class _InspectingHook:
    def __init__(self, Session) -> None:
        self.Session = Session
        self.contexts: list[CanonicalPublicationDeliveryPostSendContext] = []

    async def execute(self, context: CanonicalPublicationDeliveryPostSendContext) -> None:
        async with self.Session() as session:
            publication = await session.get(Publication, int(context.publication_id))
            lease = await session.get(PublicationDeliveryLease, int(context.publication_id))
            assert publication is not None
            assert publication.status == "sending"
            assert publication.telegram_message_ids is None
            assert publication.result_link is None
            assert lease is not None
            assert lease.lease_token == context.lease.lease_token
        self.contexts.append(context)


class _FailingHook:
    async def execute(self, context: CanonicalPublicationDeliveryPostSendContext) -> None:
        raise RuntimeError("https://api.telegram.org/botSUPERSECRET/auxiliary failed")


class _CancellingHook:
    async def execute(self, context: CanonicalPublicationDeliveryPostSendContext) -> None:
        raise asyncio.CancelledError()


async def _seed(Session, *, seed: int) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=134000 + seed,
            username=f"post-send-hook-{seed}",
            full_name=f"Post Send Hook {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100134000 + seed),
            title=f"Post Send Hook {seed}",
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
                        "text": "Canonical post-send hook proof",
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
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


def test_hook_runs_while_claim_is_sending_and_before_terminal_commit(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'post-send-hook-order.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(Session, seed=1)
            hook = _InspectingHook(Session)
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_Sender(),
                result_link_resolver=_StaticResolver(),  # type: ignore[arg-type]
                post_send_hook=hook,
            ).execute(publication_id)

            assert result.outcome == "published"
            assert result.result_link == "https://t.me/c/999/1801"
            assert result.post_send_hook_failed is False
            assert len(hook.contexts) == 1
            context = hook.contexts[0]
            assert context.publication_id == publication_id
            assert context.message_ids == (1801,)
            assert context.result_link == "https://t.me/c/999/1801"
            assert context.primary_finished_at.tzinfo is not None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.telegram_message_ids == [1801]
                assert publication.result_link == "https://t.me/c/999/1801"
                assert await session.get(PublicationDeliveryLease, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_generic_hook_failure_is_observable_but_primary_success_remains_terminal(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'post-send-hook-failure.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(Session, seed=2)
            messages: list[str] = []
            sink_id = logger.add(messages.append, format="{message}")
            try:
                result = await CanonicalPublicationDeliveryExecutor(
                    Session,
                    sender=_Sender(),
                    post_send_hook=_FailingHook(),
                ).execute(publication_id)
            finally:
                logger.remove(sink_id)

            assert result.outcome == "published"
            assert result.post_send_hook_failed is True
            rendered = "\n".join(messages)
            assert "SUPERSECRET" not in rendered
            assert "error_type=RuntimeError" in rendered
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cancelled_hook_leaves_primary_delivery_ambiguous_for_recovery(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'post-send-hook-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed(Session, seed=3)
            executor = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_Sender(),
                post_send_hook=_CancellingHook(),
            )
            with pytest.raises(asyncio.CancelledError):
                await executor.execute(publication_id)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids is None
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
