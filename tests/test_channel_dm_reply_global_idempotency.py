from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.models import Channel
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_replies import (
    ChannelDMReplyError,
    ChannelDMReplyFailure,
    ChannelDMReplyService,
)
from app.services.telegram_channel_dms import (
    TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
    TelegramChannelDMIngestionService,
)


DM_CHAT_ID = -1009401
PARENT_CHAT_ID = -1007401
BOT_USER_ID = 992
USER = {"id": 821, "is_bot": False, "first_name": "Nora", "username": "nora"}


class FakeReplyBot:
    def __init__(self, *, send_delay: float = 0) -> None:
        self.send_delay = send_delay
        self.send_calls: list[dict] = []

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=PARENT_CHAT_ID, type="channel"),
        )

    async def get_me(self):
        return SimpleNamespace(id=BOT_USER_ID)

    async def get_chat_member(self, chat_id: int, user_id: int):
        assert int(chat_id) == PARENT_CHAT_ID
        assert int(user_id) == BOT_USER_ID
        return SimpleNamespace(can_manage_direct_messages=True)

    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        return SimpleNamespace(message_id=9800 + len(self.send_calls))


def _message(message_id: int, topic_id: int) -> Message:
    return Message.model_validate(
        {
            "message_id": message_id,
            "date": datetime(2026, 8, 18, 4, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private", "is_direct_messages": True},
            "text": f"ordinary inbound DM {message_id}",
            "direct_messages_topic": {"topic_id": topic_id, "user": USER},
            "from": USER,
        }
    )


async def _seed_two_candidates(session, bot: FakeReplyBot) -> tuple[int, int]:
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical DM")
    session.add(channel)
    await session.flush()
    await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    first = await TelegramChannelDMIngestionService(session, bot=bot).ingest(_message(101, 501))
    second = await TelegramChannelDMIngestionService(session, bot=bot).ingest(_message(102, 502))
    assert first is not None and second is not None
    await session.commit()
    return int(first.reconciliation.candidate.id), int(second.reconciliation.candidate.id)


def test_same_key_cannot_be_rebound_to_a_different_candidate() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                first_id, second_id = await _seed_two_candidates(session, bot)
                service = ChannelDMReplyService(session, bot=bot)
                first = await service.execute(
                    candidate_id=first_id,
                    actor_client_id=1,
                    reply_text="first intent",
                    idempotency_key="reply-global-key-0001",
                )
                assert first.state == "sent"
                with pytest.raises(ChannelDMReplyError) as raised:
                    await service.execute(
                        candidate_id=second_id,
                        actor_client_id=1,
                        reply_text="second intent",
                        idempotency_key="reply-global-key-0001",
                    )
                assert raised.value.failure is ChannelDMReplyFailure.IDEMPOTENCY_CONFLICT
                assert len(bot.send_calls) == 1
                rows = (await session.execute(select(ChannelDMReplyCommand))).scalars().all()
                assert len(rows) == 1
                assert int(rows[0].candidate_id) == first_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_cross_candidate_same_key_has_one_global_winner(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dm-reply-global-race.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = FakeReplyBot(send_delay=0.03)
            async with Session() as seed_session:
                first_id, second_id = await _seed_two_candidates(seed_session, bot)

            async def call(candidate_id: int, text: str):
                async with Session() as session:
                    try:
                        return await ChannelDMReplyService(session, bot=bot).execute(
                            candidate_id=candidate_id,
                            actor_client_id=1,
                            reply_text=text,
                            idempotency_key="reply-global-key-0002",
                        )
                    except ChannelDMReplyError as exc:
                        return exc

            results = await asyncio.gather(
                call(first_id, "first concurrent intent"),
                call(second_id, "second concurrent intent"),
            )
            assert len(bot.send_calls) == 1
            assert sum(
                isinstance(result, ChannelDMReplyError)
                and result.failure is ChannelDMReplyFailure.IDEMPOTENCY_CONFLICT
                for result in results
            ) == 1
            async with Session() as verify:
                rows = (await verify.execute(select(ChannelDMReplyCommand))).scalars().all()
                assert len(rows) == 1
                assert rows[0].state in {"pending", "sent"}
        finally:
            await engine.dispose()

    asyncio.run(run())
