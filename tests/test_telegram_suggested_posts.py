from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_reconciliation import SourceReconciliationError
from app.services.telegram_suggested_posts import (
    SUGGESTED_POST_CONNECTOR_KIND,
    TelegramSuggestedPostDisposition,
    TelegramSuggestedPostIngestionService,
    TelegramSuggestedPostRoutingError,
    suggested_post_external_id,
)


DM_CHAT_ID = -1009001
PARENT_CHAT_ID = -1007001
USER = {"id": 501, "is_bot": False, "first_name": "Alice", "username": "alice"}


class FakeBot:
    def __init__(self, parents: dict[int, int | None]):
        self.parents = parents

    async def get_chat(self, chat_id: int):
        parent_id = self.parents.get(int(chat_id))
        parent = None if parent_id is None else SimpleNamespace(id=parent_id)
        return SimpleNamespace(id=int(chat_id), parent_chat=parent)


def _message(
    *,
    message_id: int = 77,
    text: str = "Initial proposal",
    topic_id: int = 123,
    state: str = "pending",
    price_amount: int | None = None,
    sender: dict | None = USER,
    sender_chat: dict | None = None,
) -> Message:
    info: dict[str, object] = {"state": state}
    if price_amount is not None:
        info["price"] = {"currency": "XTR", "amount": price_amount}
    payload: dict[str, object] = {
        "message_id": message_id,
        "date": datetime(2026, 8, 18, tzinfo=timezone.utc),
        "chat": {"id": DM_CHAT_ID, "type": "private"},
        "text": text,
        "direct_messages_topic": {"topic_id": topic_id, "user": USER},
        "suggested_post_info": info,
    }
    if sender is not None:
        payload["from"] = sender
    if sender_chat is not None:
        payload["sender_chat"] = sender_chat
    return Message.model_validate(payload)


def _approved_service(original: Message, *, service_message_id: int = 900) -> Message:
    return Message.model_validate(
        {
            "message_id": service_message_id,
            "date": datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private"},
            "direct_messages_topic": {"topic_id": 987, "user": USER},
            "suggested_post_approved": {
                "send_date": datetime(2026, 8, 19, tzinfo=timezone.utc),
                "price": {"currency": "XTR", "amount": 250},
                "suggested_post_message": original.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
            },
        }
    )


async def _seed_channel_and_connector(session, *, duplicate: bool = False):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical")
    session.add(channel)
    await session.flush()
    repo = SourcesRepo(session)
    first = await repo.create_connector(
        channel_id=int(channel.id),
        kind=SUGGESTED_POST_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        config={"untrusted_payload_channel_id": 999999},
    )
    if duplicate:
        await repo.create_connector(
            channel_id=int(channel.id),
            kind=SUGGESTED_POST_CONNECTOR_KIND,
            value=str(PARENT_CHAT_ID),
            mode="summary",
        )
    return channel, first


def test_suggested_post_duplicate_edit_lifecycle_and_stable_identity() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                channel, connector = await _seed_channel_and_connector(session)
                service = TelegramSuggestedPostIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                )

                initial = _message()
                first = await service.ingest(initial)
                duplicate = await service.ingest(initial)
                priced = await service.ingest(
                    _message(topic_id=456, state="approved", price_amount=100)
                )
                edited = await service.ingest(
                    _message(
                        text="Edited proposal",
                        topic_id=789,
                        state="approved",
                        price_amount=200,
                    )
                )
                lifecycle = await service.ingest(_approved_service(initial))

                documents = (await session.execute(select(SourceDocument))).scalars().all()
                candidates = (await session.execute(select(ContentCandidate))).scalars().all()
                assert len(documents) == 1
                assert len(candidates) == 1
                document = documents[0]
                candidate = candidates[0]
                assert document.external_id == suggested_post_external_id(DM_CHAT_ID, 77)
                assert document.connector_id == connector.id
                assert document.channel_id == channel.id
                assert candidate.channel_id == channel.id
                assert document.content == "Edited proposal"
                assert document.meta["telegram_direct_messages_topic"]["topic_id"] == 789
                assert document.meta["telegram_suggested_post_info"]["state"] == "approved"
                assert document.meta["telegram_suggested_post_info"]["price"]["amount"] == 200
                assert document.meta["telegram_suggested_post_lifecycle"]["event"] == "approved"
                assert document.meta["telegram_suggested_post_approved"]["event"] == "approved"
                assert first.disposition is TelegramSuggestedPostDisposition.CONTENT
                assert first.reconciliation is not None
                assert first.reconciliation.document_created is True
                assert first.reconciliation.candidate_created is True
                assert duplicate.reconciliation is not None
                assert duplicate.reconciliation.document_created is False
                assert duplicate.reconciliation.candidate_created is False
                assert priced.reconciliation is not None
                assert priced.reconciliation.document.id == document.id
                assert edited.reconciliation is not None
                assert edited.reconciliation.document.id == document.id
                assert lifecycle.disposition is TelegramSuggestedPostDisposition.LIFECYCLE
                assert lifecycle.reconciliation is not None
                assert lifecycle.reconciliation.document.id == document.id
                assert lifecycle.reconciliation.candidate.id == candidate.id
                assert lifecycle.reconciliation.document.content == "Edited proposal"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_trusted_connector_and_parent_channel_are_the_only_routing_authority() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                channel, connector = await _seed_channel_and_connector(session)
                result = await TelegramSuggestedPostIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                ).ingest(_message())
                assert result.reconciliation is not None
                assert result.reconciliation.document.connector_id == connector.id
                assert result.reconciliation.document.channel_id == channel.id
                assert result.reconciliation.candidate.channel_id == channel.id
                assert result.reconciliation.document.channel_id != 999999
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parent_mismatch_and_ambiguous_connector_mapping_fail_closed() -> None:
    async def parent_mismatch() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session)
                service = TelegramSuggestedPostIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: -1007999})
                )
                with pytest.raises(TelegramSuggestedPostRoutingError):
                    await service.ingest(_message())
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
                assert (await session.execute(select(ContentCandidate))).scalars().all() == []
        finally:
            await engine.dispose()

    async def ambiguous() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session, duplicate=True)
                service = TelegramSuggestedPostIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                )
                with pytest.raises(
                    TelegramSuggestedPostRoutingError,
                    match="ambiguous trusted Suggested Posts connector mapping",
                ):
                    await service.ingest(_message())
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
                assert (await session.execute(select(ContentCandidate))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(parent_mismatch())
    asyncio.run(ambiguous())


def test_output_lifecycle_and_retry_paths_do_not_claim_wrong_identity() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session)
                service = TelegramSuggestedPostIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                )

                ignored = await service.ingest(
                    _message(
                        sender={
                            "id": 777,
                            "is_bot": True,
                            "first_name": "PublisherBot",
                        }
                    )
                )
                assert ignored.disposition is TelegramSuggestedPostDisposition.IGNORED
                assert (await session.execute(select(SourceDocument))).scalars().all() == []

                original = _message()
                with pytest.raises(
                    SourceReconciliationError,
                    match="lifecycle projection cannot create a missing source document",
                ):
                    await service.ingest(_approved_service(original))
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
                assert (await session.execute(select(ContentCandidate))).scalars().all() == []

                real_reconcile = service.reconciler.reconcile
                attempts = 0

                async def fail_once(connector, projection):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise RuntimeError("transient ingestion failure")
                    return await real_reconcile(connector, projection)

                service.reconciler.reconcile = fail_once  # type: ignore[method-assign]
                with pytest.raises(RuntimeError, match="transient ingestion failure"):
                    await service.ingest(original)
                retried = await service.ingest(original)
                assert retried.reconciliation is not None
                assert retried.reconciliation.document_created is True
                assert retried.reconciliation.candidate_created is True
                assert attempts == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_lifecycle_without_native_correlation_is_ignored_not_guessed() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session)
                service = TelegramSuggestedPostIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                )
                event = Message.model_validate(
                    {
                        "message_id": 901,
                        "date": datetime.now(timezone.utc),
                        "chat": {"id": DM_CHAT_ID, "type": "private"},
                        "suggested_post_declined": {"comment": "No thanks"},
                    }
                )
                result = await service.ingest(event)
                assert result.disposition is TelegramSuggestedPostDisposition.IGNORED
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())
