from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.api.studio.channel_dm_replies import ChannelDMReplyLifecycleBatchRequest
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_reply_lifecycle import ChannelDMReplyLifecycleReader
from app.services.telegram_channel_dm_context import channel_dm_external_id
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


PARENT_CHAT_ID = -1007701
DM_CHAT_ID = -1009701


def test_batch_request_accepts_only_candidate_identity() -> None:
    assert ChannelDMReplyLifecycleBatchRequest.model_validate(
        {"candidate_ids": [11, 12]}
    ).model_dump() == {"candidate_ids": [11, 12]}
    for field in (
        "idempotency_key",
        "reply_text",
        "provider_message_id",
        "chat_id",
        "direct_messages_chat_id",
        "direct_messages_topic_id",
        "message_id",
        "connector_id",
        "channel_id",
        "bot_id",
        "can_manage_direct_messages",
    ):
        with pytest.raises(ValidationError):
            ChannelDMReplyLifecycleBatchRequest.model_validate(
                {"candidate_ids": [11, 12], field: 999}
            )
    with pytest.raises(ValidationError):
        ChannelDMReplyLifecycleBatchRequest.model_validate({"candidate_ids": []})


def test_batch_reader_groups_distinct_candidates_without_copying_delivery_authority() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
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

                candidates: list[ContentCandidate] = []
                documents: list[SourceDocument] = []
                for offset in (1, 2):
                    document = SourceDocument(
                        connector_id=int(connector.id),
                        channel_id=int(channel.id),
                        external_id=channel_dm_external_id(DM_CHAT_ID, 200 + offset),
                        content=f"ordinary DM {offset}",
                        content_hash=str(offset) * 64,
                        meta={
                            "transport": TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
                            "telegram_direct_messages_chat_id": DM_CHAT_ID,
                            "telegram_message_id": 200 + offset,
                            "telegram_parent_chat_id": PARENT_CHAT_ID,
                            "telegram_direct_messages_topic": {"topic_id": 700 + offset},
                        },
                    )
                    session.add(document)
                    await session.flush()
                    candidate = ContentCandidate(
                        source_document_id=int(document.id),
                        channel_id=int(channel.id),
                    )
                    session.add(candidate)
                    await session.flush()
                    command = ChannelDMReplyCommand(
                        candidate_id=int(candidate.id),
                        source_document_id=int(document.id),
                        idempotency_key=f"batch-lifecycle-{offset:04d}",
                        reply_text=f"reply {offset}",
                        state="sent" if offset == 1 else "uncertain",
                        error_class=None if offset == 1 else "provider_outcome_unknown",
                    )
                    session.add(command)
                    candidates.append(candidate)
                    documents.append(document)
                await session.commit()

                results = await ChannelDMReplyLifecycleReader(session).read_many(
                    candidate_ids=[int(candidates[0].id), int(candidates[1].id)],
                    actor_client_id=1,
                )
                assert [result.candidate_id for result in results] == [
                    int(candidates[0].id),
                    int(candidates[1].id),
                ]
                assert results[0].commands[0].reply_text == "reply 1"
                assert results[0].commands[0].state == "sent"
                assert results[1].commands[0].reply_text == "reply 2"
                assert results[1].commands[0].state == "uncertain"
                assert results[1].commands[0].error_class == "provider_outcome_unknown"
                for result in results:
                    command = result.commands[0]
                    assert not hasattr(command, "idempotency_key")
                    assert not hasattr(command, "provider_message_id")
                    assert not hasattr(command, "direct_messages_topic_id")
        finally:
            await engine.dispose()

    asyncio.run(run())
