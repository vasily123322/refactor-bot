from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import Message, ReplyParameters
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.api.studio.channel_dm_replies import (
    ChannelDMReplyIntentBatchRequest,
    ChannelDMReplyIntentEditRequest,
    ChannelDMReplyIntentEmptyRequest,
    ChannelDMReplyIntentManualRequest,
)
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.channel_dm_reply_intent import ChannelDMReplyIntent
from app.domain.content.models import ContentItem
from app.domain.models import Channel
from app.domain.publishing.models import Publication
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_reply_intents import (
    ChannelDMReplyIntentError,
    ChannelDMReplyIntentFailure,
    ChannelDMReplyIntentService,
)
from app.services.channel_dm_reply_lifecycle import ChannelDMReplyLifecycleReader
from app.services.channel_dm_reply_proposals import ChannelDMReplyProposalService
from app.services.telegram_channel_dms import (
    TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
    TelegramChannelDMIngestionService,
    is_ordinary_channel_dm,
)


DM_CHAT_ID = -1009601
PARENT_CHAT_ID = -1007601
BOT_USER_ID = 996
USER = {"id": 706, "is_bot": False, "first_name": "Ira", "username": "ira"}


class FakeReplyBot:
    def __init__(self) -> None:
        self.parent_chat_id = PARENT_CHAT_ID
        self.can_manage_direct_messages = True
        self.send_error: Exception | None = None
        self.send_calls: list[dict] = []

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=int(self.parent_chat_id), type="channel"),
        )

    async def get_me(self):
        return SimpleNamespace(id=BOT_USER_ID)

    async def get_chat_member(self, chat_id: int, user_id: int):
        assert int(chat_id) == PARENT_CHAT_ID
        assert int(user_id) == BOT_USER_ID
        return SimpleNamespace(can_manage_direct_messages=self.can_manage_direct_messages)

    async def send_message(self, **kwargs):
        self.send_calls.append(dict(kwargs))
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(message_id=9900 + len(self.send_calls))


class FakeProposalProvider:
    def __init__(self, reply_text: str = "AI durable proposal") -> None:
        self.reply_text = reply_text
        self.source_texts: list[str] = []

    async def propose(self, *, source_text: str) -> str:
        self.source_texts.append(source_text)
        return self.reply_text


class FakeProposalFactory:
    def __init__(self, provider: FakeProposalProvider) -> None:
        self.provider = provider

    async def build(self, channel_id: int) -> FakeProposalProvider:
        return self.provider


def _message(
    text: str = "ordinary inbound DM",
    *,
    message_id: int = 91,
    sender: dict | None = USER,
    edit_date: datetime | None = None,
) -> Message:
    payload: dict[str, object] = {
        "message_id": message_id,
        "date": datetime(2026, 8, 18, 7, tzinfo=timezone.utc),
        "chat": {"id": DM_CHAT_ID, "type": "private", "is_direct_messages": True},
        "text": text,
        "direct_messages_topic": {"topic_id": 421, "user": USER},
    }
    if sender is not None:
        payload["from"] = sender
    if edit_date is not None:
        payload["edit_date"] = edit_date
    return Message.model_validate(payload)


async def _seed(session, bot: FakeReplyBot, *, text: str = "ordinary inbound DM"):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Intent DM")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    ingested = await TelegramChannelDMIngestionService(session, bot=bot).ingest(_message(text))
    assert ingested is not None
    await session.commit()
    return channel, connector, ingested.reconciliation.candidate, ingested.reconciliation.document


def _proposal_service(session, provider: FakeProposalProvider) -> ChannelDMReplyProposalService:
    return ChannelDMReplyProposalService(
        session,
        provider_factory=FakeProposalFactory(provider),
    )


def _network_error() -> TelegramNetworkError:
    method = SendMessage(
        chat_id=DM_CHAT_ID,
        direct_messages_topic_id=421,
        text="reply",
        reply_parameters=ReplyParameters(message_id=91),
    )
    return TelegramNetworkError(method=method, message="connection lost")


def _bad_request() -> TelegramBadRequest:
    method = SendMessage(
        chat_id=DM_CHAT_ID,
        direct_messages_topic_id=421,
        text="reply",
        reply_parameters=ReplyParameters(message_id=91),
    )
    return TelegramBadRequest(method=method, message="reply target unavailable")


def test_intent_request_contracts_do_not_accept_delivery_or_ai_instruction_authority() -> None:
    assert ChannelDMReplyIntentManualRequest.model_validate({"reply_text": "draft"}).model_dump() == {
        "reply_text": "draft"
    }
    assert ChannelDMReplyIntentEditRequest.model_validate({"reply_text": "edited"}).model_dump() == {
        "reply_text": "edited"
    }
    assert ChannelDMReplyIntentEmptyRequest.model_validate({}).model_dump() == {}
    assert ChannelDMReplyIntentBatchRequest.model_validate({"candidate_ids": [1, 2]}).model_dump() == {
        "candidate_ids": [1, 2]
    }
    forbidden = (
        "idempotency_key",
        "handoff_idempotency_key",
        "chat_id",
        "direct_messages_chat_id",
        "direct_messages_topic_id",
        "message_id",
        "connector_id",
        "channel_id",
        "bot_id",
        "can_manage_direct_messages",
        "instruction",
        "origin",
        "source_content_hash",
        "command_id",
    )
    for field in forbidden:
        with pytest.raises(ValidationError):
            ChannelDMReplyIntentManualRequest.model_validate(
                {"reply_text": "draft", field: "override"}
            )
        with pytest.raises(ValidationError):
            ChannelDMReplyIntentEmptyRequest.model_validate({field: "override"})


def test_manual_intent_is_durable_review_state_without_delivery_or_content_side_effects() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = FakeReplyBot()
            async with Session() as session:
                _, _, candidate, document = await _seed(session, bot)
                before_hash = str(document.content_hash)
                result = await ChannelDMReplyIntentService(session, bot=bot).create_manual(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    reply_text="  durable manual reply  ",
                )
                assert result.intent.state == "pending_review"
                assert result.intent.is_current is True
                assert result.intent.origin == "manual"
                assert result.intent.reply_text == "durable manual reply"
                row = await session.get(ChannelDMReplyIntent, result.intent.intent_id)
                assert row is not None
                assert row.handoff_started_at is None
                assert row.handoff_idempotency_key is None
                assert row.consumed_command_id is None
                assert bot.send_calls == []
                assert (await session.execute(select(ChannelDMReplyCommand))).scalars().all() == []
                assert (await session.execute(select(ContentItem))).scalars().all() == []
                assert (await session.execute(select(Publication))).scalars().all() == []
                assert (await session.execute(select(CandidateRewriteRun))).scalars().all() == []
                assert str(document.content_hash) == before_hash
                candidate_id = int(candidate.id)
                intent_id = result.intent.intent_id

            async with Session() as reloaded:
                history = await ChannelDMReplyIntentService(reloaded, bot=bot).read(
                    candidate_id=candidate_id,
                    actor_client_id=1,
                )
                assert history.intents[0].intent_id == intent_id
                assert history.intents[0].state == "pending_review"
                assert history.intents[0].reply_text == "durable manual reply"
                assert (await reloaded.execute(select(ChannelDMReplyCommand))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ai_intent_reuses_t54_redacted_context_and_plain_t54_proposal_stays_ephemeral() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(
                    session,
                    bot,
                    text="Please check password=supersecretvalue before replying",
                )
                ephemeral_provider = FakeProposalProvider("Ephemeral only")
                ephemeral = await _proposal_service(session, ephemeral_provider).propose(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                )
                assert ephemeral.reply_text == "Ephemeral only"
                assert ephemeral_provider.source_texts == [
                    "Please check password=[REDACTED] before replying"
                ]
                assert (await session.execute(select(ChannelDMReplyIntent))).scalars().all() == []

                provider = FakeProposalProvider("Persisted AI proposal")
                result = await ChannelDMReplyIntentService(
                    session,
                    bot=bot,
                    proposal_service=_proposal_service(session, provider),
                ).create_ai(candidate_id=int(candidate.id), actor_client_id=1)
                assert result.intent.origin == "ai"
                assert result.intent.state == "pending_review"
                assert provider.source_texts == [
                    "Please check password=[REDACTED] before replying"
                ]
                row = await session.get(ChannelDMReplyIntent, result.intent.intent_id)
                assert row is not None and row.handoff_idempotency_key is None
                assert bot.send_calls == []
                assert (await session.execute(select(ChannelDMReplyCommand))).scalars().all() == []
                assert (await session.execute(select(ContentItem))).scalars().all() == []
                assert (await session.execute(select(Publication))).scalars().all() == []
                assert (await session.execute(select(CandidateRewriteRun))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["telegram_suggested_posts", "rss"])
def test_intent_creation_hard_rejects_suggested_posts_and_non_dm(transport: str) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, connector, candidate, document = await _seed(session, bot)
                connector.kind = transport
                document.meta = {**dict(document.meta or {}), "transport": transport}
                await session.commit()
                with pytest.raises(ChannelDMReplyIntentError):
                    await ChannelDMReplyIntentService(session, bot=bot).create_manual(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                        reply_text="must not become a generic DM intent",
                    )
                assert (await session.execute(select(ChannelDMReplyIntent))).scalars().all() == []
                assert (await session.execute(select(ChannelDMReplyCommand))).scalars().all() == []
                assert bot.send_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_intent_edit_and_dismiss_survive_reload_without_command_creation() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = FakeReplyBot()
            async with Session() as session:
                _, _, candidate, _ = await _seed(session, bot)
                service = ChannelDMReplyIntentService(session, bot=bot)
                created = await service.create_manual(
                    candidate_id=int(candidate.id), actor_client_id=1, reply_text="first"
                )
                edited = await service.edit(
                    intent_id=created.intent.intent_id,
                    actor_client_id=1,
                    reply_text="edited durable text",
                )
                assert edited.reply_text == "edited durable text"
                intent_id = edited.intent_id
                candidate_id = int(candidate.id)
            async with Session() as reloaded:
                service = ChannelDMReplyIntentService(reloaded, bot=bot)
                history = await service.read(candidate_id=candidate_id, actor_client_id=1)
                assert history.intents[0].reply_text == "edited durable text"
                dismissed = await service.dismiss(intent_id=intent_id, actor_client_id=1)
                assert dismissed.state == "dismissed"
            async with Session() as reloaded_again:
                history = await ChannelDMReplyIntentService(reloaded_again, bot=bot).read(
                    candidate_id=candidate_id, actor_client_id=1
                )
                assert history.intents[0].state == "dismissed"
                assert (await reloaded_again.execute(select(ChannelDMReplyCommand))).scalars().all() == []
                assert bot.send_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_native_source_edit_makes_old_intent_stale_and_send_creates_no_command() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = FakeReplyBot()
            async with Session() as session:
                _, _, candidate, document = await _seed(session, bot, text="DM v1")
                created = await ChannelDMReplyIntentService(session, bot=bot).create_manual(
                    candidate_id=int(candidate.id), actor_client_id=1, reply_text="reply for v1"
                )
                old_hash = str(document.content_hash)
                candidate_id = int(candidate.id)
                intent_id = created.intent.intent_id

                edited = await TelegramChannelDMIngestionService(session, bot=bot).ingest(
                    _message(
                        "DM v2",
                        edit_date=datetime(2026, 8, 18, 7, 5, tzinfo=timezone.utc),
                    )
                )
                assert edited is not None
                assert str(edited.reconciliation.document.content_hash) != old_hash

            async with Session() as reloaded:
                service = ChannelDMReplyIntentService(reloaded, bot=bot)
                with pytest.raises(ChannelDMReplyIntentError) as raised:
                    await service.send(intent_id=intent_id, actor_client_id=1)
                assert raised.value.failure is ChannelDMReplyIntentFailure.STALE
                history = await service.read(candidate_id=candidate_id, actor_client_id=1)
                assert history.intents[0].state == "stale"
                row = await reloaded.get(ChannelDMReplyIntent, intent_id)
                assert row is not None and row.handoff_idempotency_key is None
                assert (await reloaded.execute(select(ChannelDMReplyCommand))).scalars().all() == []
                assert bot.send_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_explicit_intent_send_allocates_hidden_random_key_and_same_intent_never_resends() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                service = ChannelDMReplyIntentService(session, bot=bot)
                created = await service.create_manual(
                    candidate_id=int(candidate.id), actor_client_id=1, reply_text="explicit intent send"
                )
                pre_send = await session.get(ChannelDMReplyIntent, created.intent.intent_id)
                assert pre_send is not None and pre_send.handoff_idempotency_key is None

                first = await service.send(
                    intent_id=created.intent.intent_id,
                    actor_client_id=1,
                )
                assert first.intent.state == "consumed"
                assert first.command.state == "sent"
                assert first.intent.consumed_command_id == first.command.command_id
                assert len(bot.send_calls) == 1
                command = (await session.execute(select(ChannelDMReplyCommand))).scalar_one()
                intent = await session.get(ChannelDMReplyIntent, created.intent.intent_id)
                assert intent is not None
                assert intent.handoff_idempotency_key == command.idempotency_key
                assert command.idempotency_key.startswith("dm-reply-intent:")
                assert command.idempotency_key != f"dm-reply-intent:{created.intent.intent_id}"
                assert len(command.idempotency_key) > len("dm-reply-intent:") + 20
                assert command.reply_text == "explicit intent send"

                repeated = await service.send(
                    intent_id=created.intent.intent_id,
                    actor_client_id=1,
                )
                assert repeated.command.command_id == first.command.command_id
                assert repeated.command.reused_existing is True
                assert len(bot.send_calls) == 1
                assert len((await session.execute(select(ChannelDMReplyCommand))).scalars().all()) == 1

                lifecycle = await ChannelDMReplyLifecycleReader(session).read(
                    candidate_id=int(candidate.id), actor_client_id=1
                )
                assert lifecycle.commands[0].command_id == first.command.command_id
                assert lifecycle.commands[0].state == "sent"

                second_intent = await service.create_manual(
                    candidate_id=int(candidate.id), actor_client_id=1, reply_text="separate reply"
                )
                second = await service.send(
                    intent_id=second_intent.intent.intent_id,
                    actor_client_id=1,
                )
                assert second.command.command_id != first.command.command_id
                assert len(bot.send_calls) == 2
                keys = {
                    row.idempotency_key
                    for row in (await session.execute(select(ChannelDMReplyCommand))).scalars().all()
                }
                assert len(keys) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("provider_error", "expected_state"),
    [(_bad_request, "failed"), (_network_error, "uncertain")],
)
def test_consumed_intent_never_copies_delivery_state(provider_error, expected_state: str) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                created = await ChannelDMReplyIntentService(session, bot=bot).create_manual(
                    candidate_id=int(candidate.id), actor_client_id=1, reply_text="delivery separation"
                )
                bot.send_error = provider_error()
                sent = await ChannelDMReplyIntentService(session, bot=bot).send(
                    intent_id=created.intent.intent_id,
                    actor_client_id=1,
                )
                assert sent.intent.state == "consumed"
                assert sent.command.state == expected_state
                assert sent.intent.state not in {"sent", "failed", "uncertain"}
                lifecycle = await ChannelDMReplyLifecycleReader(session).read(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                )
                assert lifecycle.commands[0].state == expected_state
                assert len(bot.send_calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_deleting_consumed_intent_does_not_delete_delivery_command() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                service = ChannelDMReplyIntentService(session, bot=bot)
                created = await service.create_manual(
                    candidate_id=int(candidate.id), actor_client_id=1, reply_text="audit linkage"
                )
                sent = await service.send(intent_id=created.intent.intent_id, actor_client_id=1)
                intent_row = await session.get(ChannelDMReplyIntent, created.intent.intent_id)
                assert intent_row is not None
                command_id = sent.command.command_id
                await session.delete(intent_row)
                await session.commit()
                assert await session.get(ChannelDMReplyCommand, command_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_outgoing_bot_authored_message_still_cannot_reenter_ordinary_dm_ingress() -> None:
    outgoing = _message(
        "bot authored reply",
        message_id=777,
        sender={"id": BOT_USER_ID, "is_bot": True, "first_name": "Bot"},
    )
    assert is_ordinary_channel_dm(outgoing) is False
