from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.content.models import ContentItem
from app.domain.models import Channel
from app.domain.publishing.models import Publication
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_reply_proposals import (
    ChannelDMReplyProposalError,
    ChannelDMReplyProposalFailure,
    ChannelDMReplyProposalService,
)
from app.services.telegram_channel_dm_context import channel_dm_external_id
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


DM_CHAT_ID = -1009501
PARENT_CHAT_ID = -1007501


class FakeProposalProvider:
    model = "fake-reply-model"

    def __init__(self, reply_text: str = "Suggested reply") -> None:
        self.reply_text = reply_text
        self.source_texts: list[str] = []

    async def propose(self, *, source_text: str) -> str:
        self.source_texts.append(source_text)
        return self.reply_text


class FakeProposalFactory:
    def __init__(self, provider: FakeProposalProvider) -> None:
        self.provider = provider
        self.channel_ids: list[int] = []

    async def build(self, channel_id: int) -> FakeProposalProvider:
        self.channel_ids.append(int(channel_id))
        return self.provider


async def _seed(session, *, transport: str = TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical DM")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=transport,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    document = SourceDocument(
        connector_id=int(connector.id),
        channel_id=int(channel.id),
        external_id=channel_dm_external_id(DM_CHAT_ID, 111),
        content="Can you send me the details tomorrow?",
        content_hash="c" * 64,
        meta={
            "transport": transport,
            "telegram_direct_messages_chat_id": DM_CHAT_ID,
            "telegram_message_id": 111,
            "telegram_parent_chat_id": PARENT_CHAT_ID,
            "telegram_direct_messages_topic": {"topic_id": 611},
        },
    )
    session.add(document)
    await session.flush()
    candidate = ContentCandidate(
        source_document_id=int(document.id),
        channel_id=int(channel.id),
    )
    session.add(candidate)
    await session.commit()
    return channel, connector, document, candidate


def test_ai_proposal_returns_editable_text_without_creating_send_or_content_authority() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                channel, _, _, candidate = await _seed(session)
                provider = FakeProposalProvider("Sure — I can send the details tomorrow.")
                factory = FakeProposalFactory(provider)
                before = (
                    len((await session.execute(select(ChannelDMReplyCommand))).scalars().all()),
                    len((await session.execute(select(ContentItem))).scalars().all()),
                    len((await session.execute(select(Publication))).scalars().all()),
                    len((await session.execute(select(CandidateRewriteRun))).scalars().all()),
                )

                result = await ChannelDMReplyProposalService(
                    session,
                    provider_factory=factory,
                ).propose(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                )

                assert result.candidate_id == int(candidate.id)
                assert result.reply_text == "Sure — I can send the details tomorrow."
                assert result.model == "fake-reply-model"
                assert factory.channel_ids == [int(channel.id)]
                assert provider.source_texts == ["Can you send me the details tomorrow?"]
                after = (
                    len((await session.execute(select(ChannelDMReplyCommand))).scalars().all()),
                    len((await session.execute(select(ContentItem))).scalars().all()),
                    len((await session.execute(select(Publication))).scalars().all()),
                    len((await session.execute(select(CandidateRewriteRun))).scalars().all()),
                )
                assert after == before == (0, 0, 0, 0)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["telegram_suggested_posts", "rss"])
def test_ai_proposal_rejects_non_ordinary_dm_transport_before_provider(transport: str) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, _, candidate = await _seed(session, transport=transport)
                provider = FakeProposalProvider()
                factory = FakeProposalFactory(provider)
                with pytest.raises(ChannelDMReplyProposalError) as raised:
                    await ChannelDMReplyProposalService(
                        session,
                        provider_factory=factory,
                    ).propose(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                    )
                assert raised.value.failure == ChannelDMReplyProposalFailure.ROUTING_MISMATCH
                assert factory.channel_ids == []
                assert provider.source_texts == []
                assert (await session.execute(select(ChannelDMReplyCommand))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ai_proposal_fails_closed_on_malformed_native_provenance() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, document, candidate = await _seed(session)
                meta = dict(document.meta or {})
                meta.pop("telegram_direct_messages_topic", None)
                document.meta = meta
                await session.commit()
                provider = FakeProposalProvider()
                factory = FakeProposalFactory(provider)
                with pytest.raises(ChannelDMReplyProposalError) as raised:
                    await ChannelDMReplyProposalService(
                        session,
                        provider_factory=factory,
                    ).propose(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                    )
                assert raised.value.failure == ChannelDMReplyProposalFailure.MALFORMED_PROVENANCE
                assert factory.channel_ids == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ai_proposal_owner_boundary_is_not_a_candidate_or_ai_oracle() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, _, candidate = await _seed(session)
                factory = FakeProposalFactory(FakeProposalProvider())
                with pytest.raises(ChannelDMReplyProposalError) as raised:
                    await ChannelDMReplyProposalService(
                        session,
                        provider_factory=factory,
                    ).propose(
                        candidate_id=int(candidate.id),
                        actor_client_id=999,
                    )
                assert raised.value.failure == ChannelDMReplyProposalFailure.CANDIDATE_NOT_FOUND
                assert factory.channel_ids == []
        finally:
            await engine.dispose()

    asyncio.run(run())
