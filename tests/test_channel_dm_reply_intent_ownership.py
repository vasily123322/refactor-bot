from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.channel_dm_reply_intent import ChannelDMReplyIntent
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_reply_intents import (
    ChannelDMReplyIntentError,
    ChannelDMReplyIntentFailure,
    ChannelDMReplyIntentService,
)
from app.services.channel_dm_reply_proposals import ChannelDMReplyProposalService
from app.services.telegram_channel_dm_context import channel_dm_external_id
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


DM_CHAT_ID = -1009901
PARENT_CHAT_ID = -1007901


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
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(can_manage_direct_messages=True)

    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        return SimpleNamespace(message_id=1)


class ProposalProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def propose(self, *, source_text: str) -> str:
        self.calls.append(source_text)
        return "automation wants to replace this"


class ProposalFactory:
    def __init__(self, provider: ProposalProvider) -> None:
        self.provider = provider

    async def build(self, channel_id: int) -> ProposalProvider:
        return self.provider


async def _seed(session):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Owner scoped DM")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    content = "owner scoped inbound"
    document = SourceDocument(
        connector_id=int(connector.id),
        channel_id=int(channel.id),
        external_id=channel_dm_external_id(DM_CHAT_ID, 121),
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        meta={
            "transport": TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
            "telegram_direct_messages_chat_id": DM_CHAT_ID,
            "telegram_message_id": 121,
            "telegram_parent_chat_id": PARENT_CHAT_ID,
            "telegram_direct_messages_topic": {"topic_id": 521},
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
    return channel, candidate


def test_active_intent_is_owner_scoped_and_automation_cannot_clobber_human_review() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = NoSendBot()
                channel, candidate = await _seed(session)
                first = await ChannelDMReplyIntentService(session, bot=bot).create_manual(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    reply_text="owner one review",
                )

                channel.owner_id = 2
                await session.commit()
                second = await ChannelDMReplyIntentService(session, bot=bot).create_manual(
                    candidate_id=int(candidate.id),
                    actor_client_id=2,
                    reply_text="owner two review",
                )
                assert first.intent.intent_id != second.intent.intent_id

                owner_two = await ChannelDMReplyIntentService(session, bot=bot).read(
                    candidate_id=int(candidate.id), actor_client_id=2
                )
                assert [row.intent_id for row in owner_two.intents] == [second.intent.intent_id]
                with pytest.raises(ChannelDMReplyIntentError) as old_owner:
                    await ChannelDMReplyIntentService(session, bot=bot).read(
                        candidate_id=int(candidate.id), actor_client_id=1
                    )
                assert old_owner.value.failure is ChannelDMReplyIntentFailure.CANDIDATE_NOT_FOUND

                provider = ProposalProvider()
                proposal_service = ChannelDMReplyProposalService(
                    session,
                    provider_factory=ProposalFactory(provider),
                )
                with pytest.raises(ChannelDMReplyIntentError) as blocked:
                    await ChannelDMReplyIntentService(
                        session,
                        bot=bot,
                        proposal_service=proposal_service,
                    ).create_ai(
                        candidate_id=int(candidate.id),
                        actor_client_id=2,
                        origin="automation",
                    )
                assert blocked.value.failure is ChannelDMReplyIntentFailure.NOT_REVIEWABLE
                assert provider.calls == ["owner scoped inbound"]

                rows = (
                    await session.execute(
                        select(ChannelDMReplyIntent).order_by(ChannelDMReplyIntent.id)
                    )
                ).scalars().all()
                assert len(rows) == 2
                assert rows[1].owner_client_id == 2
                assert rows[1].origin == "manual"
                assert rows[1].proposed_text == "owner two review"
                assert (await session.execute(select(ChannelDMReplyCommand))).scalars().all() == []
                assert bot.send_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
