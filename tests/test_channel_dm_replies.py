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
from app.api.studio.channel_dm_replies import ChannelDMReplyRequest
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.content.models import ContentItem
from app.domain.models import Channel
from app.domain.publishing.models import Publication
from app.domain.sources.models import ContentCandidate, SourceConnector, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_replies import (
    ChannelDMReplyError,
    ChannelDMReplyFailure,
    ChannelDMReplyService,
)
from app.services.telegram_channel_dms import (
    TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
    TelegramChannelDMIngestionService,
    is_ordinary_channel_dm,
)


DM_CHAT_ID = -1009201
PARENT_CHAT_ID = -1007201
BOT_USER_ID = 990
USER = {"id": 701, "is_bot": False, "first_name": "Dana", "username": "dana"}


class FakeReplyBot:
    def __init__(
        self,
        *,
        parent_chat_id: int = PARENT_CHAT_ID,
        can_manage_direct_messages: bool = True,
        send_delay: float = 0,
    ) -> None:
        self.parent_chat_id = parent_chat_id
        self.can_manage_direct_messages = can_manage_direct_messages
        self.send_delay = send_delay
        self.send_error: Exception | None = None
        self.events: list[str] = []
        self.send_calls: list[dict] = []

    async def get_chat(self, chat_id: int):
        self.events.append("get_chat")
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=int(self.parent_chat_id), type="channel"),
        )

    async def get_me(self):
        self.events.append("get_me")
        return SimpleNamespace(id=BOT_USER_ID)

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.events.append("get_chat_member")
        assert int(chat_id) == int(self.parent_chat_id)
        assert int(user_id) == BOT_USER_ID
        return SimpleNamespace(can_manage_direct_messages=self.can_manage_direct_messages)

    async def send_message(self, **kwargs):
        self.events.append("send_message")
        self.send_calls.append(dict(kwargs))
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(message_id=9001 + len(self.send_calls))


def _message(*, message_id: int = 81, sender: dict | None = USER) -> Message:
    payload: dict[str, object] = {
        "message_id": message_id,
        "date": datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
        "chat": {"id": DM_CHAT_ID, "type": "private", "is_direct_messages": True},
        "text": "ordinary inbound DM",
        "direct_messages_topic": {"topic_id": 321, "user": USER},
    }
    if sender is not None:
        payload["from"] = sender
    return Message.model_validate(payload)


async def _seed(session, bot: FakeReplyBot):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical DM")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    ingested = await TelegramChannelDMIngestionService(session, bot=bot).ingest(_message())
    assert ingested is not None
    await session.commit()
    return channel, connector, ingested.reconciliation.candidate, ingested.reconciliation.document


def _network_error() -> TelegramNetworkError:
    method = SendMessage(
        chat_id=DM_CHAT_ID,
        direct_messages_topic_id=321,
        text="reply",
        reply_parameters=ReplyParameters(message_id=81),
    )
    return TelegramNetworkError(method=method, message="connection lost")


def _bad_request() -> TelegramBadRequest:
    method = SendMessage(
        chat_id=DM_CHAT_ID,
        direct_messages_topic_id=321,
        text="reply",
        reply_parameters=ReplyParameters(message_id=81),
    )
    return TelegramBadRequest(method=method, message="reply target unavailable")


def test_valid_reply_derives_all_native_routing_and_persists_provider_message() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                bot.events.clear()
                result = await ChannelDMReplyService(session, bot=bot).execute(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    reply_text="  Native reply  ",
                    idempotency_key="reply-key-0001",
                )
                assert result.state == "sent"
                assert result.provider_message_id == 9002
                assert result.reused_existing is False
                assert bot.events == ["get_chat", "get_me", "get_chat_member", "send_message"]
                assert len(bot.send_calls) == 1
                call = bot.send_calls[0]
                assert call["chat_id"] == DM_CHAT_ID
                assert call["direct_messages_topic_id"] == 321
                assert call["text"] == "Native reply"
                assert call["parse_mode"] is None
                assert call["reply_parameters"].message_id == 81
                assert "message_thread_id" not in call
                row = (await session.execute(select(ChannelDMReplyCommand))).scalar_one()
                assert row.state == "sent"
                assert row.provider_message_id == 9002
                assert row.reply_text == "Native reply"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_request_schema_rejects_all_client_native_authority_overrides() -> None:
    for field in (
        "chat_id", "direct_messages_chat_id", "direct_messages_topic_id",
        "message_thread_id", "message_id", "connector_id", "channel_id", "bot_id", "parse_mode",
    ):
        with pytest.raises(ValidationError):
            ChannelDMReplyRequest.model_validate(
                {"reply_text": "hello", "idempotency_key": "reply-key-0002", field: 999}
            )


@pytest.mark.parametrize("text", ["", "   ", "x" * 4097])
def test_invalid_text_is_rejected_before_provider_or_command(text: str) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                bot.events.clear()
                with pytest.raises(ChannelDMReplyError) as raised:
                    await ChannelDMReplyService(session, bot=bot).execute(
                        candidate_id=int(candidate.id), actor_client_id=1,
                        reply_text=text, idempotency_key="reply-key-0003",
                    )
                assert raised.value.failure is ChannelDMReplyFailure.INVALID_REQUEST
                assert bot.send_calls == []
                assert (await session.execute(select(ChannelDMReplyCommand))).scalars().all() == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_suggested_post_transport_is_hard_rejected_server_side() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, connector, candidate, document = await _seed(session, bot)
                connector.kind = "telegram_suggested_posts"
                document.meta = {**dict(document.meta or {}), "transport": "telegram_suggested_posts"}
                await session.commit()
                bot.events.clear()
                with pytest.raises(ChannelDMReplyError) as raised:
                    await ChannelDMReplyService(session, bot=bot).execute(
                        candidate_id=int(candidate.id), actor_client_id=1,
                        reply_text="must not send", idempotency_key="reply-key-0004",
                    )
                assert raised.value.failure is ChannelDMReplyFailure.ROUTING_MISMATCH
                assert bot.send_calls == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_disabled_ambiguous_and_missing_connector_fail_closed_before_send() -> None:
    async def case(kind: str) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                channel, connector, candidate, _ = await _seed(session, bot)
                if kind == "disabled":
                    connector.enabled = False
                elif kind == "ambiguous":
                    await SourcesRepo(session).create_connector(
                        channel_id=int(channel.id), kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
                        value=str(PARENT_CHAT_ID), mode="summary",
                    )
                else:
                    await session.delete(connector)
                await session.commit()
                bot.events.clear()
                with pytest.raises(ChannelDMReplyError):
                    await ChannelDMReplyService(session, bot=bot).execute(
                        candidate_id=int(candidate.id), actor_client_id=1,
                        reply_text="no", idempotency_key=f"reply-key-{kind}",
                    )
                assert bot.send_calls == []
        finally:
            await engine.dispose()
    for kind in ("disabled", "ambiguous", "missing"):
        asyncio.run(case(kind))


def test_stored_parent_identity_and_topic_provenance_fail_closed() -> None:
    async def case(kind: str) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, document = await _seed(session, bot)
                meta = dict(document.meta or {})
                if kind == "parent":
                    meta["telegram_parent_chat_id"] = PARENT_CHAT_ID - 1
                elif kind == "identity":
                    document.external_id = "dm:wrong:identity"
                else:
                    meta.pop("telegram_direct_messages_topic", None)
                document.meta = meta
                await session.commit()
                bot.events.clear()
                with pytest.raises(ChannelDMReplyError):
                    await ChannelDMReplyService(session, bot=bot).execute(
                        candidate_id=int(candidate.id), actor_client_id=1,
                        reply_text="no", idempotency_key=f"reply-key-bad-{kind}",
                    )
                assert bot.send_calls == []
        finally:
            await engine.dispose()
    for kind in ("parent", "identity", "topic"):
        asyncio.run(case(kind))


def test_fresh_parent_and_manage_dm_right_are_checked_before_every_new_send() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot(can_manage_direct_messages=False)
                _, _, candidate, _ = await _seed(session, bot)
                bot.events.clear()
                result = await ChannelDMReplyService(session, bot=bot).execute(
                    candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="no right", idempotency_key="reply-key-0005",
                )
                assert result.state == "failed"
                assert result.error_class == "insufficient_rights"
                assert bot.events == ["get_chat", "get_me", "get_chat_member"]
                assert bot.send_calls == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_parent_drift_fails_before_rights_and_provider() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                bot.parent_chat_id = PARENT_CHAT_ID - 9
                bot.events.clear()
                result = await ChannelDMReplyService(session, bot=bot).execute(
                    candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="drift", idempotency_key="reply-key-0006",
                )
                assert result.state == "failed"
                assert result.error_class == "routing_mismatch"
                assert bot.events == ["get_chat"]
                assert bot.send_calls == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_same_key_reuses_sent_result_without_second_provider_or_rights_call() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                service = ChannelDMReplyService(session, bot=bot)
                first = await service.execute(
                    candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="once", idempotency_key="reply-key-0007",
                )
                events_after_first = list(bot.events)
                second = await service.execute(
                    candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="once", idempotency_key="reply-key-0007",
                )
                assert first.state == second.state == "sent"
                assert second.reused_existing is True
                assert len(bot.send_calls) == 1
                assert bot.events == events_after_first
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_same_key_with_different_text_is_an_idempotency_conflict() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                service = ChannelDMReplyService(session, bot=bot)
                await service.execute(candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="first", idempotency_key="reply-key-0008")
                with pytest.raises(ChannelDMReplyError) as raised:
                    await service.execute(candidate_id=int(candidate.id), actor_client_id=1,
                        reply_text="different", idempotency_key="reply-key-0008")
                assert raised.value.failure is ChannelDMReplyFailure.IDEMPOTENCY_CONFLICT
                assert len(bot.send_calls) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_distinct_keys_allow_two_intentional_identical_replies() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                service = ChannelDMReplyService(session, bot=bot)
                for key in ("reply-key-0009-a", "reply-key-0009-b"):
                    result = await service.execute(candidate_id=int(candidate.id), actor_client_id=1,
                        reply_text="same text", idempotency_key=key)
                    assert result.state == "sent"
                assert len(bot.send_calls) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_provider_rejection_is_failed_and_never_falls_back_or_resends() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                bot.send_error = _bad_request()
                service = ChannelDMReplyService(session, bot=bot)
                first = await service.execute(candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="rejected", idempotency_key="reply-key-0010")
                second = await service.execute(candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="rejected", idempotency_key="reply-key-0010")
                assert first.state == second.state == "failed"
                assert first.error_class == "provider_rejected"
                assert second.reused_existing is True
                assert len(bot.send_calls) == 1
                assert bot.send_calls[0]["direct_messages_topic_id"] == 321
                assert "message_thread_id" not in bot.send_calls[0]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_network_ambiguity_becomes_uncertain_and_same_key_never_resends() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                bot.send_error = _network_error()
                service = ChannelDMReplyService(session, bot=bot)
                first = await service.execute(candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="uncertain", idempotency_key="reply-key-0011")
                bot.send_error = None
                second = await service.execute(candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="uncertain", idempotency_key="reply-key-0011")
                assert first.state == second.state == "uncertain"
                assert first.error_class == "provider_outcome_unknown"
                assert second.reused_existing is True
                assert len(bot.send_calls) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_concurrent_same_key_has_one_durable_winner_and_one_provider_mutation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dm-reply-race.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = FakeReplyBot(send_delay=0.03)
            async with Session() as seed_session:
                _, _, candidate, _ = await _seed(seed_session, bot)
                candidate_id = int(candidate.id)
            bot.events.clear()
            async def call():
                async with Session() as session:
                    return await ChannelDMReplyService(session, bot=bot).execute(
                        candidate_id=candidate_id, actor_client_id=1,
                        reply_text="race", idempotency_key="reply-key-0012",
                    )
            results = await asyncio.gather(call(), call())
            assert len(bot.send_calls) == 1
            assert sum(result.reused_existing for result in results) == 1
            async with Session() as verify:
                rows = (await verify.execute(select(ChannelDMReplyCommand))).scalars().all()
                assert len(rows) == 1
                assert rows[0].state in {"pending", "sent"}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_reply_redacts_credentials_before_durable_persistence_and_provider() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                await ChannelDMReplyService(session, bot=bot).execute(
                    candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="password=supersecretvalue", idempotency_key="reply-key-0013",
                )
                row = (await session.execute(select(ChannelDMReplyCommand))).scalar_one()
                assert row.reply_text == "password=[REDACTED]"
                assert bot.send_calls[0]["text"] == "password=[REDACTED]"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_outgoing_bot_reply_cannot_reenter_t5_1_and_content_authorities_stay_unchanged() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeReplyBot()
                _, _, candidate, _ = await _seed(session, bot)
                before = (
                    len((await session.execute(select(ContentItem))).scalars().all()),
                    len((await session.execute(select(Publication))).scalars().all()),
                    len((await session.execute(select(CandidateRewriteRun))).scalars().all()),
                )
                result = await ChannelDMReplyService(session, bot=bot).execute(
                    candidate_id=int(candidate.id), actor_client_id=1,
                    reply_text="outgoing", idempotency_key="reply-key-0014",
                )
                assert result.state == "sent"
                after = (
                    len((await session.execute(select(ContentItem))).scalars().all()),
                    len((await session.execute(select(Publication))).scalars().all()),
                    len((await session.execute(select(CandidateRewriteRun))).scalars().all()),
                )
                assert after == before
                output = _message(sender={"id": BOT_USER_ID, "is_bot": True, "first_name": "Bot"})
                assert is_ordinary_channel_dm(output) is False
                assert await TelegramChannelDMIngestionService(session, bot=bot).ingest(output) is None
                candidates = (await session.execute(select(ContentCandidate))).scalars().all()
                documents = (await session.execute(select(SourceDocument))).scalars().all()
                assert len(candidates) == 1
                assert len(documents) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
