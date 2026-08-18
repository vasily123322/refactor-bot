from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register ORM metadata
from app.api.studio.channel_dm_replies import (
    ChannelDMReplyLifecycleCommandResponse,
    ChannelDMReplyLifecycleResponse,
)
from app.core.db import Base
from app.domain.channel_dm_reply import ChannelDMReplyCommand
from app.domain.content.models import ContentItem
from app.domain.models import Channel
from app.domain.publishing.models import Publication
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.domain.sources.rewrite import CandidateRewriteRun
from app.repositories.sources_v2 import SourcesRepo
from app.services.channel_dm_reply_lifecycle import (
    ChannelDMReplyLifecycleError,
    ChannelDMReplyLifecycleFailure,
    ChannelDMReplyLifecycleReader,
)
from app.services.channel_dm_reply_proposals import ChannelDMReplyProposalService
from app.services.telegram_channel_dm_context import channel_dm_external_id
from app.services.telegram_channel_dms import TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND


DM_CHAT_ID = -1009601
PARENT_CHAT_ID = -1007601


class FakeProposalProvider:
    async def propose(self, *, source_text: str) -> str:
        assert source_text == "ordinary inbound DM"
        return "Editable AI draft"


class FakeProposalFactory:
    async def build(self, channel_id: int) -> FakeProposalProvider:
        assert int(channel_id) > 0
        return FakeProposalProvider()


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
        external_id=channel_dm_external_id(DM_CHAT_ID, 121),
        content="ordinary inbound DM",
        content_hash="d" * 64,
        meta={
            "transport": transport,
            "telegram_direct_messages_chat_id": DM_CHAT_ID,
            "telegram_message_id": 121,
            "telegram_parent_chat_id": PARENT_CHAT_ID,
            "telegram_direct_messages_topic": {"topic_id": 621},
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


async def _add_command(
    session,
    *,
    candidate: ContentCandidate,
    document: SourceDocument,
    key: str,
    text: str,
    state: str,
    error_class: str | None = None,
    provider_message_id: int | None = None,
) -> ChannelDMReplyCommand:
    finished_at = None if state == "pending" else datetime.now(timezone.utc)
    command = ChannelDMReplyCommand(
        candidate_id=int(candidate.id),
        source_document_id=int(document.id),
        idempotency_key=key,
        reply_text=text,
        state=state,
        provider_message_id=provider_message_id,
        error_class=error_class,
        dispatch_started_at=datetime.now(timezone.utc),
        finished_at=finished_at,
    )
    session.add(command)
    await session.commit()
    await session.refresh(command)
    return command


def test_sent_failed_uncertain_and_pending_survive_reload_without_transition(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dm-lifecycle.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, connector, document, candidate = await _seed(session)
                candidate_id = int(candidate.id)
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key="lifecycle-sent-0001",
                    text="confirmed reply",
                    state="sent",
                    provider_message_id=8101,
                )
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key="lifecycle-failed-0002",
                    text="definite failure",
                    state="failed",
                    error_class="provider_rejected",
                )
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key="lifecycle-uncertain-0003",
                    text="ambiguous delivery",
                    state="uncertain",
                    error_class="provider_outcome_unknown",
                )
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key="lifecycle-pending-0004",
                    text="in flight evidence",
                    state="pending",
                )
                # Historical delivery evidence must remain readable after mutable
                # source routing is disabled; readback is not a new send authority.
                connector.enabled = False
                await session.commit()

            async with Session() as reloaded:
                result = await ChannelDMReplyLifecycleReader(reloaded).read(
                    candidate_id=candidate_id,
                    actor_client_id=1,
                )
                assert [command.state for command in result.commands] == [
                    "pending",
                    "uncertain",
                    "failed",
                    "sent",
                ]
                by_state = {command.state: command for command in result.commands}
                assert by_state["sent"].sent_at is not None
                assert by_state["sent"].finished_at is not None
                assert by_state["failed"].sent_at is None
                assert by_state["uncertain"].state == "uncertain"
                assert by_state["uncertain"].error_class == "provider_outcome_unknown"
                assert by_state["pending"].finished_at is None

                persisted = (
                    await reloaded.execute(
                        select(ChannelDMReplyCommand).order_by(ChannelDMReplyCommand.id)
                    )
                ).scalars().all()
                assert [row.state for row in persisted] == [
                    "sent",
                    "failed",
                    "uncertain",
                    "pending",
                ]
                assert len(persisted) == 4
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_lifecycle_read_model_is_narrow_and_excludes_delivery_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dm-lifecycle-view.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, document, candidate = await _seed(session)
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key="private-idempotency-key-0001",
                    text="browser-safe persisted reply",
                    state="sent",
                    provider_message_id=8201,
                )
                result = await ChannelDMReplyLifecycleReader(session).read(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                )
                command = result.commands[0]
                response = ChannelDMReplyLifecycleResponse(
                    candidate_id=result.candidate_id,
                    commands=[
                        ChannelDMReplyLifecycleCommandResponse(
                            command_id=command.command_id,
                            reply_text=command.reply_text,
                            state=command.state,
                            requested_at=command.requested_at,
                            sent_at=command.sent_at,
                            finished_at=command.finished_at,
                            error_class=command.error_class,
                        )
                    ],
                ).model_dump()
                item = response["commands"][0]
                assert set(item) == {
                    "command_id",
                    "reply_text",
                    "state",
                    "requested_at",
                    "sent_at",
                    "finished_at",
                    "error_class",
                }
                for forbidden in (
                    "idempotency_key",
                    "provider_message_id",
                    "source_document_id",
                    "chat_id",
                    "direct_messages_chat_id",
                    "direct_messages_topic_id",
                    "message_id",
                    "connector_id",
                    "channel_id",
                    "bot_id",
                    "can_manage_direct_messages",
                ):
                    assert forbidden not in item
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_history_is_bounded_distinguishable_and_newest_first() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, document, candidate = await _seed(session)
                created_ids: list[int] = []
                for index in range(7):
                    command = await _add_command(
                        session,
                        candidate=candidate,
                        document=document,
                        key=f"lifecycle-distinct-{index:04d}",
                        text=f"explicit reply {index}",
                        state="sent",
                        provider_message_id=8300 + index,
                    )
                    created_ids.append(int(command.id))
                result = await ChannelDMReplyLifecycleReader(session).read(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    limit=99,
                )
                assert len(result.commands) == 5
                assert [command.command_id for command in result.commands] == list(
                    reversed(created_ids[-5:])
                )
                assert len({command.reply_text for command in result.commands}) == 5
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["telegram_suggested_posts", "rss"])
def test_non_ordinary_dm_has_no_reply_lifecycle(transport: str) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, document, candidate = await _seed(session, transport=transport)
                # Even corrupted/manual ledger data cannot make a non-DM candidate
                # acquire the ordinary Channel-DM lifecycle read authority.
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key=f"lifecycle-nondm-{transport}",
                    text="must stay hidden",
                    state="sent",
                )
                with pytest.raises(ChannelDMReplyLifecycleError) as raised:
                    await ChannelDMReplyLifecycleReader(session).read(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                    )
                assert raised.value.failure is ChannelDMReplyLifecycleFailure.ROUTING_MISMATCH
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_lifecycle_owner_boundary_hides_candidate_and_history() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, document, candidate = await _seed(session)
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key="lifecycle-owner-0001",
                    text="private reply",
                    state="sent",
                )
                with pytest.raises(ChannelDMReplyLifecycleError) as raised:
                    await ChannelDMReplyLifecycleReader(session).read(
                        candidate_id=int(candidate.id),
                        actor_client_id=999,
                    )
                assert raised.value.failure is ChannelDMReplyLifecycleFailure.CANDIDATE_NOT_FOUND
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unknown_internal_error_is_reduced_to_safe_browser_classification() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, document, candidate = await _seed(session)
                await _add_command(
                    session,
                    candidate=candidate,
                    document=document,
                    key="lifecycle-safe-error-0001",
                    text="safe reply",
                    state="failed",
                    error_class="raw-provider-secret-detail",
                )
                result = await ChannelDMReplyLifecycleReader(session).read(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                )
                assert result.commands[0].error_class == "delivery_error"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ai_proposal_and_lifecycle_read_are_command_and_domain_mutation_free() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                _, _, document, candidate = await _seed(session)
                candidate_id = int(candidate.id)
                source_identity = (
                    int(document.id),
                    int(document.connector_id),
                    str(document.external_id),
                    dict(document.meta or {}),
                )
                candidate_identity = (
                    int(candidate.id),
                    int(candidate.source_document_id),
                    int(candidate.channel_id),
                    str(candidate.status),
                )

                proposal = await ChannelDMReplyProposalService(
                    session,
                    provider_factory=FakeProposalFactory(),
                ).propose(candidate_id=candidate_id, actor_client_id=1)
                assert proposal.reply_text == "Editable AI draft"
                lifecycle = await ChannelDMReplyLifecycleReader(session).read(
                    candidate_id=candidate_id,
                    actor_client_id=1,
                )
                assert lifecycle.commands == ()

                assert (await session.execute(select(ChannelDMReplyCommand))).scalars().all() == []
                assert (await session.execute(select(ContentItem))).scalars().all() == []
                assert (await session.execute(select(Publication))).scalars().all() == []
                assert (await session.execute(select(CandidateRewriteRun))).scalars().all() == []
                await session.refresh(document)
                await session.refresh(candidate)
                assert (
                    int(document.id),
                    int(document.connector_id),
                    str(document.external_id),
                    dict(document.meta or {}),
                ) == source_identity
                assert (
                    int(candidate.id),
                    int(candidate.source_document_id),
                    int(candidate.channel_id),
                    str(candidate.status),
                ) == candidate_identity
        finally:
            await engine.dispose()

    asyncio.run(run())
