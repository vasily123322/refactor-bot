from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.core.db import Base
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_replies import ChannelDMReplyError, ChannelDMReplyFailure, ChannelDMReplyService
from app.services.telegram_channel_dms import (
    TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
    TelegramChannelDMIngestionService,
)


DM_CHAT_ID = -1009301
PARENT_CHAT_ID = -1007301
USER = {"id": 811, "is_bot": False, "first_name": "Mira", "username": "mira"}


class FakeBot:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=PARENT_CHAT_ID, type="channel"),
        )

    async def get_me(self):
        return SimpleNamespace(id=991)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(can_manage_direct_messages=True)

    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        return SimpleNamespace(message_id=9901)


def _ordinary_message() -> Message:
    return Message.model_validate(
        {
            "message_id": 91,
            "date": datetime(2026, 8, 18, 3, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private", "is_direct_messages": True},
            "text": "ordinary inbound DM",
            "direct_messages_topic": {"topic_id": 431, "user": USER},
            "from": USER,
        }
    )


def test_non_dm_transport_is_rejected_before_any_native_send() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical")
                session.add(channel)
                await session.flush()
                connector = await SourcesRepo(session).create_connector(
                    channel_id=int(channel.id),
                    kind="rss",
                    value="https://example.test/feed.xml",
                    mode="summary",
                )
                document = SourceDocument(
                    connector_id=int(connector.id),
                    channel_id=int(channel.id),
                    external_id="rss:item:1",
                    content="not a DM",
                    content_hash="a" * 64,
                    meta={"transport": "rss"},
                )
                session.add(document)
                await session.flush()
                candidate = ContentCandidate(
                    source_document_id=int(document.id),
                    channel_id=int(channel.id),
                )
                session.add(candidate)
                await session.commit()

                bot = FakeBot()
                try:
                    await ChannelDMReplyService(session, bot=bot).execute(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                        reply_text="must not send",
                        idempotency_key="reply-key-non-dm",
                    )
                except ChannelDMReplyError as exc:
                    assert exc.failure is ChannelDMReplyFailure.ROUTING_MISMATCH
                else:
                    raise AssertionError("non-DM candidate unexpectedly became reply authority")
                assert bot.send_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ordinary_dm_reply_does_not_mutate_suggested_post_lifecycle() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeBot()
                channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical")
                session.add(channel)
                await session.flush()
                ordinary = await SourcesRepo(session).create_connector(
                    channel_id=int(channel.id),
                    kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
                    value=str(PARENT_CHAT_ID),
                    mode="rewrite",
                    reuse_policy="rewrite_with_attribution",
                )
                ingested = await TelegramChannelDMIngestionService(session, bot=bot).ingest(
                    _ordinary_message()
                )
                assert ingested is not None

                suggested = await SourcesRepo(session).create_connector(
                    channel_id=int(channel.id),
                    kind="telegram_suggested_posts",
                    value=str(PARENT_CHAT_ID),
                    mode="rewrite",
                )
                lifecycle = {
                    "transport": "telegram_suggested_posts",
                    "telegram_suggested_post_lifecycle": {
                        "event": "paid",
                        "service_message_id": 7001,
                    },
                }
                suggested_document = SourceDocument(
                    connector_id=int(suggested.id),
                    channel_id=int(channel.id),
                    external_id="dm:-1009301:7000",
                    content="suggested",
                    content_hash="b" * 64,
                    meta=lifecycle,
                )
                session.add(suggested_document)
                await session.commit()
                before = dict(suggested_document.meta or {})

                result = await ChannelDMReplyService(session, bot=bot).execute(
                    candidate_id=int(ingested.reconciliation.candidate.id),
                    actor_client_id=1,
                    reply_text="ordinary reply",
                    idempotency_key="reply-key-isolation",
                )
                assert result.state == "sent"
                await session.refresh(suggested_document)
                assert suggested_document.meta == before
                assert int(ordinary.id) == int(ingested.reconciliation.document.connector_id)
        finally:
            await engine.dispose()

    asyncio.run(run())
