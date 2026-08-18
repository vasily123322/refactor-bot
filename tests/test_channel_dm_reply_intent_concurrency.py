from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.channel_dm_reply_intent import ChannelDMReplyIntent
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_reply_intents import ChannelDMReplyIntentService
from app.services.telegram_channel_dm_context import channel_dm_external_id
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


DM_CHAT_ID = -1009701
PARENT_CHAT_ID = -1007701
BOT_USER_ID = 997


class SlowReplyBot:
    def __init__(self, *, delay: float = 0.08) -> None:
        self.delay = delay
        self.send_calls: list[dict] = []
        self.send_started = asyncio.Event()

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=PARENT_CHAT_ID, type="channel"),
        )

    async def get_me(self):
        return SimpleNamespace(id=BOT_USER_ID)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(can_manage_direct_messages=True)

    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        self.send_started.set()
        await asyncio.sleep(self.delay)
        return SimpleNamespace(message_id=12000 + len(self.send_calls))


async def _seed(session):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Concurrent DM")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    content = "concurrent inbound v1"
    document = SourceDocument(
        connector_id=int(connector.id),
        channel_id=int(channel.id),
        external_id=channel_dm_external_id(DM_CHAT_ID, 101),
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        published_at=datetime(2026, 8, 18, 8, tzinfo=timezone.utc),
        meta={
            "transport": TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
            "telegram_direct_messages_chat_id": DM_CHAT_ID,
            "telegram_message_id": 101,
            "telegram_parent_chat_id": PARENT_CHAT_ID,
            "telegram_direct_messages_topic": {
                "topic_id": 501,
                "user": {"id": 801, "is_bot": False, "first_name": "Concurrent"},
            },
        },
    )
    session.add(document)
    await session.flush()
    candidate = ContentCandidate(
        source_document_id=int(document.id),
        channel_id=int(channel.id),
        status="new",
    )
    session.add(candidate)
    await session.commit()
    return candidate, document


def test_overlapping_two_tab_send_converges_on_one_t53_command(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'two-tab.db'}",
            connect_args={"timeout": 10},
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = SlowReplyBot()
            async with Session() as seed_session:
                candidate, _ = await _seed(seed_session)
                created = await ChannelDMReplyIntentService(seed_session, bot=bot).create_manual(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    reply_text="one explicit user intent",
                )
                intent_id = created.intent.intent_id

            async with Session() as first_session, Session() as second_session:
                first_service = ChannelDMReplyIntentService(first_session, bot=bot)
                second_service = ChannelDMReplyIntentService(second_session, bot=bot)
                first_task = asyncio.create_task(
                    first_service.send(intent_id=intent_id, actor_client_id=1)
                )
                await asyncio.wait_for(bot.send_started.wait(), timeout=2)
                second_task = asyncio.create_task(
                    second_service.send(intent_id=intent_id, actor_client_id=1)
                )
                first, second = await asyncio.gather(first_task, second_task)
                assert first.command.command_id == second.command.command_id

            async with Session() as audit:
                commands = (await audit.execute(select(ChannelDMReplyCommand))).scalars().all()
                intent = await audit.get(ChannelDMReplyIntent, intent_id)
                assert len(commands) == 1
                assert len(bot.send_calls) == 1
                assert intent is not None
                assert intent.state == "consumed"
                assert intent.handoff_idempotency_key == commands[0].idempotency_key
                assert int(intent.consumed_command_id or 0) == int(commands[0].id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_send_winner_then_native_edit_is_serialized_without_second_command(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'source-race.db'}",
            connect_args={"timeout": 10},
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = SlowReplyBot(delay=0.12)
            async with Session() as seed_session:
                candidate, document = await _seed(seed_session)
                created = await ChannelDMReplyIntentService(seed_session, bot=bot).create_manual(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    reply_text="current before edit",
                )
                intent_id = created.intent.intent_id
                document_id = int(document.id)

            async with Session() as send_session, Session() as edit_session:
                send_task = asyncio.create_task(
                    ChannelDMReplyIntentService(send_session, bot=bot).send(
                        intent_id=intent_id,
                        actor_client_id=1,
                    )
                )
                await asyncio.wait_for(bot.send_started.wait(), timeout=2)
                document = await edit_session.get(SourceDocument, document_id)
                assert document is not None
                document.content = "concurrent inbound v2"
                document.content_hash = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
                await edit_session.commit()
                result = await send_task
                assert result.intent.state == "consumed"

            async with Session() as audit:
                commands = (await audit.execute(select(ChannelDMReplyCommand))).scalars().all()
                intent = await audit.get(ChannelDMReplyIntent, intent_id)
                document = await audit.get(SourceDocument, document_id)
                assert len(commands) == 1
                assert len(bot.send_calls) == 1
                assert intent is not None and intent.state == "consumed"
                assert document is not None and document.content == "concurrent inbound v2"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reload_recovers_reserved_handoff_link_without_provider_mutation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recover.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = SlowReplyBot(delay=0)
            async with Session() as session:
                candidate, document = await _seed(session)
                created = await ChannelDMReplyIntentService(session, bot=bot).create_manual(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    reply_text="recover this handoff",
                )
                intent = await session.get(ChannelDMReplyIntent, created.intent.intent_id)
                assert intent is not None
                handoff_key = "dm-reply-intent:0123456789abcdef0123456789abcdef"
                intent.handoff_started_at = datetime.now(timezone.utc)
                intent.handoff_idempotency_key = handoff_key
                command = ChannelDMReplyCommand(
                    candidate_id=int(candidate.id),
                    source_document_id=int(document.id),
                    idempotency_key=handoff_key,
                    reply_text="recover this handoff",
                    state="pending",
                )
                session.add(command)
                await session.commit()
                candidate_id = int(candidate.id)
                intent_id = int(intent.id)
                command_id = int(command.id)

            async with Session() as reloaded:
                history = await ChannelDMReplyIntentService(reloaded, bot=bot).read(
                    candidate_id=candidate_id,
                    actor_client_id=1,
                )
                recovered = next(row for row in history.intents if row.intent_id == intent_id)
                assert recovered.state == "consumed"
                assert recovered.consumed_command_id == command_id
                assert bot.send_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
