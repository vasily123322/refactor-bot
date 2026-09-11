from __future__ import annotations

import asyncio
import hashlib
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


DM_CHAT_ID = -1009801
PARENT_CHAT_ID = -1007801


class NoSendBot:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=PARENT_CHAT_ID, type="channel"),
        )

    async def get_me(self):
        return SimpleNamespace(id=998)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(can_manage_direct_messages=True)

    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        return SimpleNamespace(message_id=1)


async def _seed(session):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Review race")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    content = "review race source"
    document = SourceDocument(
        connector_id=int(connector.id),
        channel_id=int(channel.id),
        external_id=channel_dm_external_id(DM_CHAT_ID, 111),
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        meta={
            "transport": TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
            "telegram_direct_messages_chat_id": DM_CHAT_ID,
            "telegram_message_id": 111,
            "telegram_parent_chat_id": PARENT_CHAT_ID,
            "telegram_direct_messages_topic": {"topic_id": 511},
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
    return candidate


def test_concurrent_edit_and_dismiss_remain_review_only(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'review-race.db'}",
            connect_args={"timeout": 10},
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = NoSendBot()
            async with Session() as seed_session:
                candidate = await _seed(seed_session)
                created = await ChannelDMReplyIntentService(seed_session, bot=bot).create_manual(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    reply_text="original review text",
                )
                intent_id = created.intent.intent_id

            async with Session() as edit_session, Session() as dismiss_session:
                outcomes = await asyncio.gather(
                    ChannelDMReplyIntentService(edit_session, bot=bot).edit(
                        intent_id=intent_id,
                        actor_client_id=1,
                        reply_text="edited review text",
                    ),
                    ChannelDMReplyIntentService(dismiss_session, bot=bot).dismiss(
                        intent_id=intent_id,
                        actor_client_id=1,
                    ),
                    return_exceptions=True,
                )
                assert any(not isinstance(outcome, Exception) for outcome in outcomes)

            async with Session() as audit:
                intent = await audit.get(ChannelDMReplyIntent, intent_id)
                commands = (await audit.execute(select(ChannelDMReplyCommand))).scalars().all()
                assert intent is not None
                assert intent.state in {"pending_review", "dismissed"}
                if intent.state == "pending_review":
                    assert intent.proposed_text == "edited review text"
                assert intent.state != "consumed"
                assert intent.consumed_command_id is None
                assert commands == []
                assert bot.send_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
