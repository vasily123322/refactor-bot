from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.content.models import ContentItem
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.candidate_current_structured_rewrite import (
    CandidateCurrentStructuredRewriteError,
    CandidateCurrentStructuredRewriteService,
)
from app.services.candidate_rewrite import candidate_rewrite_input_hash
from app.services.candidate_structured_rewrite_ai import STRUCTURED_REWRITE_GENERATION_KIND
from app.services.telegram_channel_dms import (
    TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
    TelegramChannelDMIngestionService,
    TelegramChannelDMRoutingError,
    channel_dm_external_id,
    is_ordinary_channel_dm,
)


DM_CHAT_ID = -1009101
PARENT_CHAT_ID = -1007101
USER = {"id": 601, "is_bot": False, "first_name": "Bob", "username": "bob"}


class FakeBot:
    def __init__(self, parents: dict[int, int | None]):
        self.parents = parents
        self.get_chat_calls: list[int] = []

    async def get_chat(self, chat_id: int):
        self.get_chat_calls.append(int(chat_id))
        parent_id = self.parents.get(int(chat_id))
        parent = (
            None
            if parent_id is None
            else SimpleNamespace(id=int(parent_id), type="channel")
        )
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=parent,
        )


def _message(
    *,
    message_id: int = 81,
    text: str = "Ordinary direct message v1",
    topic_id: int = 321,
    sender: dict | None = USER,
    topic_user: dict | None = USER,
    sender_chat: dict | None = None,
    media_group_id: str | None = None,
    reply_to_message_id: int | None = None,
    edit_date: datetime | None = None,
) -> Message:
    payload: dict[str, object] = {
        "message_id": message_id,
        "date": datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
        "chat": {
            "id": DM_CHAT_ID,
            "type": "private",
            "is_direct_messages": True,
        },
        "text": text,
        "direct_messages_topic": {"topic_id": topic_id, "user": topic_user},
    }
    if sender is not None:
        payload["from"] = sender
    if sender_chat is not None:
        payload["sender_chat"] = sender_chat
    if media_group_id is not None:
        payload["media_group_id"] = media_group_id
    if edit_date is not None:
        payload["edit_date"] = edit_date
    if reply_to_message_id is not None:
        payload["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "date": datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
            "chat": {
                "id": DM_CHAT_ID,
                "type": "private",
                "is_direct_messages": True,
            },
            "text": "Earlier message",
            "from": USER,
        }
    return Message.model_validate(payload)


async def _seed_channel_and_connector(
    session,
    *,
    with_connector: bool = True,
    duplicate: bool = False,
    config: dict | None = None,
):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical DM")
    session.add(channel)
    await session.flush()
    connector = None
    if with_connector:
        connector = await SourcesRepo(session).create_connector(
            channel_id=int(channel.id),
            kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
            value=str(PARENT_CHAT_ID),
            mode="rewrite",
            reuse_policy="rewrite_with_attribution",
            config=config or {},
        )
        if duplicate:
            await SourcesRepo(session).create_connector(
                channel_id=int(channel.id),
                kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
                value=str(PARENT_CHAT_ID),
                mode="summary",
            )
    return channel, connector


def _post_document(text: str) -> dict:
    return {
        "schema_version": 1,
        "mode": "rich",
        "blocks": [{"id": "p", "type": "paragraph", "content": text}],
        "telegram": {},
        "metadata": {},
    }


def test_ordinary_dm_duplicate_edit_provenance_repair_and_no_side_effects() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                channel, connector = await _seed_channel_and_connector(
                    session,
                    config={"untrusted_internal_channel_id": 999999},
                )
                assert connector is not None
                bot = FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                service = TelegramChannelDMIngestionService(session, bot=bot)

                initial = _message(reply_to_message_id=70, media_group_id="album-a")
                first = await service.ingest(initial)
                duplicate = await service.ingest(initial)
                edited = await service.ingest(
                    _message(
                        text="Ordinary direct message v2",
                        topic_id=999,
                        reply_to_message_id=71,
                        media_group_id="album-b",
                        edit_date=datetime(2026, 8, 18, 3, tzinfo=timezone.utc),
                    )
                )
                assert first is not None and duplicate is not None and edited is not None

                documents = (await session.execute(select(SourceDocument))).scalars().all()
                candidates = (await session.execute(select(ContentCandidate))).scalars().all()
                assert len(documents) == 1
                assert len(candidates) == 1
                document = documents[0]
                candidate = candidates[0]
                assert document.id == first.reconciliation.document.id
                assert document.id == duplicate.reconciliation.document.id
                assert document.id == edited.reconciliation.document.id
                assert candidate.id == first.reconciliation.candidate.id
                assert candidate.id == duplicate.reconciliation.candidate.id
                assert candidate.id == edited.reconciliation.candidate.id
                assert document.external_id == channel_dm_external_id(DM_CHAT_ID, 81)
                assert document.content == "Ordinary direct message v2"
                assert document.connector_id == connector.id
                assert document.channel_id == channel.id
                assert candidate.channel_id == channel.id
                assert document.channel_id != 999999
                assert document.meta["telegram_direct_messages_topic"]["topic_id"] == 999
                assert document.meta["telegram_reply_to"] == {
                    "chat_id": DM_CHAT_ID,
                    "message_id": 71,
                }
                assert document.meta["telegram_media_group_id"] == "album-b"
                assert first.reconciliation.document_created is True
                assert first.reconciliation.candidate_created is True
                assert duplicate.reconciliation.document_created is False
                assert duplicate.reconciliation.candidate_created is False
                assert edited.reconciliation.document_created is False
                assert edited.reconciliation.candidate_created is False

                inbox_rows = await SourcesRepo(session).list_candidate_rows(
                    int(channel.id), status="new", limit=100
                )
                assert [(row_candidate.id, row_document.id) for row_candidate, row_document in inbox_rows] == [
                    (candidate.id, document.id)
                ]
                assert (await session.execute(select(ContentItem))).scalars().all() == []
                assert (await session.execute(select(CandidateRewriteRun))).scalars().all() == []
                assert bot.get_chat_calls == [DM_CHAT_ID, DM_CHAT_ID, DM_CHAT_ID]

                await session.execute(delete(ContentCandidate))
                await session.commit()
                repaired = await service.ingest(initial)
                assert repaired is not None
                assert repaired.reconciliation.document.id == document.id
                assert repaired.reconciliation.document_created is False
                assert repaired.reconciliation.candidate_created is True
                repaired_candidates = (
                    await session.execute(select(ContentCandidate)
                )).scalars().all()
                assert len(repaired_candidates) == 1
                assert repaired_candidates[0].source_document_id == document.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_feature_classifier_and_routing_fail_closed() -> None:
    assert TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND != "telegram"
    ordinary = _message()
    assert is_ordinary_channel_dm(ordinary) is True
    assert is_ordinary_channel_dm(
        ordinary.model_copy(update={"suggested_post_info": SimpleNamespace(state="pending")})
    ) is False
    assert is_ordinary_channel_dm(
        ordinary.model_copy(update={"suggested_post_declined": SimpleNamespace(comment="no")})
    ) is False
    assert is_ordinary_channel_dm(
        _message(sender={"id": 777, "is_bot": True, "first_name": "Bot"})
    ) is False
    assert is_ordinary_channel_dm(
        _message(sender_chat={"id": -1001, "type": "channel", "title": "Publisher"})
    ) is False
    assert is_ordinary_channel_dm(
        _message(topic_user={"id": 999, "is_bot": False, "first_name": "Other"})
    ) is False

    async def missing() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session, with_connector=False)
                with pytest.raises(TelegramChannelDMRoutingError, match="NOT_CONFIGURED"):
                    await TelegramChannelDMIngestionService(
                        session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                    ).ingest(_message())
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
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
                with pytest.raises(TelegramChannelDMRoutingError, match="AMBIGUOUS"):
                    await TelegramChannelDMIngestionService(
                        session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                    ).ingest(_message())
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
        finally:
            await engine.dispose()

    async def parent_mismatch() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session)
                with pytest.raises(TelegramChannelDMRoutingError, match="CHANNEL_NOT_FOUND"):
                    await TelegramChannelDMIngestionService(
                        session, bot=FakeBot({DM_CHAT_ID: -1007999})
                    ).ingest(_message())
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(missing())
    asyncio.run(ambiguous())
    asyncio.run(parent_mismatch())


def test_reply_and_media_group_are_provenance_not_identity_and_album_members_stay_distinct() -> None:
    assert channel_dm_external_id(DM_CHAT_ID, 91) == channel_dm_external_id(DM_CHAT_ID, 91)
    assert channel_dm_external_id(DM_CHAT_ID, 91) != channel_dm_external_id(DM_CHAT_ID, 92)

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session)
                service = TelegramChannelDMIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                )
                first = await service.ingest(
                    _message(
                        message_id=91,
                        text="Album member one",
                        reply_to_message_id=40,
                        media_group_id="album-1",
                    )
                )
                same = await service.ingest(
                    _message(
                        message_id=91,
                        text="Album member one edited",
                        reply_to_message_id=41,
                        media_group_id="album-2",
                    )
                )
                second = await service.ingest(
                    _message(
                        message_id=92,
                        text="Album member two",
                        reply_to_message_id=40,
                        media_group_id="album-1",
                    )
                )
                assert first is not None and same is not None and second is not None
                assert first.reconciliation.document.id == same.reconciliation.document.id
                assert first.reconciliation.candidate.id == same.reconciliation.candidate.id
                assert first.reconciliation.document.id != second.reconciliation.document.id
                documents = (await session.execute(select(SourceDocument))).scalars().all()
                assert len(documents) == 2
                by_external = {document.external_id: document for document in documents}
                first_document = by_external[channel_dm_external_id(DM_CHAT_ID, 91)]
                assert first_document.meta["telegram_reply_to"]["message_id"] == 41
                assert first_document.meta["telegram_media_group_id"] == "album-2"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reconciliation_failure_is_retryable() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await _seed_channel_and_connector(session)
                service = TelegramChannelDMIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                )
                real_reconcile = service.reconciler.reconcile
                attempts = 0

                async def fail_once(connector, projection):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise RuntimeError("transient dm reconciliation failure")
                    return await real_reconcile(connector, projection)

                service.reconciler.reconcile = fail_once  # type: ignore[method-assign]
                with pytest.raises(RuntimeError, match="transient dm reconciliation failure"):
                    await service.ingest(_message())
                assert (await session.execute(select(SourceDocument))).scalars().all() == []
                retried = await service.ingest(_message())
                assert retried is not None
                assert retried.reconciliation.document_created is True
                assert retried.reconciliation.candidate_created is True
                assert attempts == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_native_dm_edit_makes_old_t3_structured_rewrite_non_current_and_unapplicable() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                channel, _ = await _seed_channel_and_connector(session)
                service = TelegramChannelDMIngestionService(
                    session, bot=FakeBot({DM_CHAT_ID: PARENT_CHAT_ID})
                )
                v1 = await service.ingest(_message(text="DM source version one"))
                assert v1 is not None
                document = v1.reconciliation.document
                candidate = v1.reconciliation.candidate
                original_hash = document.content_hash

                run = CandidateRewriteRun(
                    candidate_id=int(candidate.id),
                    provider="channel_ai_structured",
                    model="model-v1",
                    status="completed",
                    input_hash=candidate_rewrite_input_hash(
                        document,
                        candidate,
                        "rewrite_with_attribution",
                    ),
                    input_chars=len(document.content),
                    text="Rewrite based on v1",
                    output={
                        "generation_kind": STRUCTURED_REWRITE_GENERATION_KIND,
                        "post_document": _post_document("Rewrite based on v1"),
                    },
                )
                session.add(run)
                await session.flush()
                candidate.meta = {
                    **dict(candidate.meta or {}),
                    "rewrite_run_id": int(run.id),
                    "rewrite_provider": "channel_ai_structured",
                    "rewrite_model": "model-v1",
                }
                await session.commit()
                assert await CandidateCurrentStructuredRewriteService(session).current(
                    channel_id=int(channel.id),
                    candidate_id=int(candidate.id),
                ) is not None

                v2 = await service.ingest(
                    _message(
                        text="DM source version two",
                        edit_date=datetime(2026, 8, 18, 4, tzinfo=timezone.utc),
                    )
                )
                assert v2 is not None
                assert v2.reconciliation.document.id == document.id
                assert v2.reconciliation.candidate.id == candidate.id
                assert v2.reconciliation.document.content == "DM source version two"
                assert v2.reconciliation.document.content_hash != original_hash
                assert await CandidateCurrentStructuredRewriteService(session).current(
                    channel_id=int(channel.id),
                    candidate_id=int(candidate.id),
                ) is None

                with pytest.raises(
                    CandidateCurrentStructuredRewriteError,
                    match="no longer current",
                ):
                    await CandidateCurrentStructuredRewriteService(session).apply(
                        channel_id=int(channel.id),
                        candidate_id=int(candidate.id),
                        expected_run_id=int(run.id),
                    )
                assert (await session.execute(select(ContentItem))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())
