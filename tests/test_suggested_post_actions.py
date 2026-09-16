from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import ApproveSuggestedPost
from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.studio.suggested_post_actions import SuggestedPostActionRequest
from app.core.db import Base
from app.domain.models import Channel
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.suggested_post_actions import (
    SuggestedPostAction,
    SuggestedPostActionError,
    SuggestedPostActionFailure,
    SuggestedPostActionService,
)
from app.services.telegram_suggested_posts import (
    SUGGESTED_POST_CONNECTOR_KIND,
    TelegramSuggestedPostIngestionService,
)


DM_CHAT_ID = -1009001
PARENT_CHAT_ID = -1007001
BOT_USER_ID = 999
USER = {"id": 501, "is_bot": False, "first_name": "Alice", "username": "alice"}


class FakeActionBot:
    def __init__(
        self,
        *,
        parent_chat_id: int = PARENT_CHAT_ID,
        can_post_messages: bool = True,
        can_manage_direct_messages: bool = True,
        action_delay: float = 0,
    ) -> None:
        self.parent_chat_id = parent_chat_id
        self.can_post_messages = can_post_messages
        self.can_manage_direct_messages = can_manage_direct_messages
        self.action_delay = action_delay
        self.approve_error: Exception | None = None
        self.decline_error: Exception | None = None
        self.get_chat_calls: list[int] = []
        self.get_member_calls: list[tuple[int, int]] = []
        self.approve_calls: list[tuple[int, int]] = []
        self.decline_calls: list[tuple[int, int, str | None]] = []

    async def get_chat(self, chat_id: int):
        self.get_chat_calls.append(int(chat_id))
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=int(self.parent_chat_id)),
        )

    async def get_me(self):
        return SimpleNamespace(id=BOT_USER_ID)

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.get_member_calls.append((int(chat_id), int(user_id)))
        return SimpleNamespace(
            can_post_messages=self.can_post_messages,
            can_manage_direct_messages=self.can_manage_direct_messages,
        )

    async def approve_suggested_post(self, *, chat_id: int, message_id: int) -> bool:
        self.approve_calls.append((int(chat_id), int(message_id)))
        if self.action_delay:
            await asyncio.sleep(self.action_delay)
        if self.approve_error is not None:
            raise self.approve_error
        return True

    async def decline_suggested_post(
        self,
        *,
        chat_id: int,
        message_id: int,
        comment: str | None = None,
    ) -> bool:
        self.decline_calls.append((int(chat_id), int(message_id), comment))
        if self.action_delay:
            await asyncio.sleep(self.action_delay)
        if self.decline_error is not None:
            raise self.decline_error
        return True


def _proposal() -> Message:
    return Message.model_validate(
        {
            "message_id": 77,
            "date": datetime(2026, 8, 18, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private"},
            "from": USER,
            "text": "Initial Suggested Post",
            "direct_messages_topic": {"topic_id": 123, "user": USER},
            "suggested_post_info": {"state": "pending"},
        }
    )


def _approved_service(original: Message) -> Message:
    return Message.model_validate(
        {
            "message_id": 900,
            "date": datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private"},
            "suggested_post_approved": {
                "send_date": int(datetime(2026, 8, 18, 1, tzinfo=timezone.utc).timestamp()),
                "suggested_post_message": original.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
            },
        }
    )


async def _seed(session, bot: FakeActionBot):
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical")
    session.add(channel)
    await session.flush()
    connector = await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=SUGGESTED_POST_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
        reuse_policy="rewrite_with_attribution",
    )
    ingested = await TelegramSuggestedPostIngestionService(session, bot=bot).ingest(_proposal())
    assert ingested.reconciliation is not None
    candidate = ingested.reconciliation.candidate
    document = ingested.reconciliation.document
    candidate.meta = {
        **dict(candidate.meta or {}),
        "rewrite_run_id": 4242,
        "rewrite_provider": "channel_ai_structured",
        "rewrite_model": "model-v1",
    }
    await session.commit()
    return channel, connector, candidate, document


async def _rows(session):
    candidate = (await session.execute(select(ContentCandidate))).scalar_one()
    document = (await session.execute(select(SourceDocument))).scalar_one()
    return candidate, document


def test_pending_approve_derives_identity_checks_fresh_rights_and_reconciles_same_candidate() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot()
                _, _, candidate, document = await _seed(session, bot)
                original_candidate_id = int(candidate.id)
                original_document_id = int(document.id)

                result = await SuggestedPostActionService(session, bot=bot).execute(
                    candidate_id=original_candidate_id,
                    actor_client_id=1,
                    action=SuggestedPostAction.APPROVE,
                )

                assert bot.approve_calls == [(DM_CHAT_ID, 77)]
                assert bot.decline_calls == []
                assert bot.get_chat_calls[-1] == DM_CHAT_ID
                assert bot.get_member_calls[-1] == (PARENT_CHAT_ID, BOT_USER_ID)
                assert int(result.candidate.id) == original_candidate_id
                assert int(result.document.id) == original_document_id
                assert result.document.meta["telegram_suggested_post_lifecycle"]["event"] == "approved"
                assert result.document.meta["telegram_suggested_post_lifecycle"]["origin"] == "studio_native_action_result"
                assert result.candidate.status == "new"
                assert result.candidate.content_item_id is None
                assert result.candidate.meta["rewrite_run_id"] == 4242
                assert result.candidate.meta["rewrite_provider"] == "channel_ai_structured"
                assert result.candidate.meta["rewrite_model"] == "model-v1"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pending_decline_derives_identity_uses_manage_dm_right_and_preserves_t3_state() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot(can_post_messages=False, can_manage_direct_messages=True)
                _, _, candidate, document = await _seed(session, bot)

                result = await SuggestedPostActionService(session, bot=bot).execute(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    action=SuggestedPostAction.DECLINE,
                    comment="Not for this channel",
                )

                assert bot.approve_calls == []
                assert bot.decline_calls == [(DM_CHAT_ID, 77, "Not for this channel")]
                assert bot.get_member_calls[-1] == (PARENT_CHAT_ID, BOT_USER_ID)
                assert int(result.document.id) == int(document.id)
                lifecycle = result.document.meta["telegram_suggested_post_lifecycle"]
                assert lifecycle["event"] == "declined"
                assert lifecycle["payload"]["comment"] == "Not for this channel"
                assert result.candidate.meta["rewrite_run_id"] == 4242
                assert result.candidate.content_item_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("action", "can_post", "can_manage"),
    [
        (SuggestedPostAction.APPROVE, False, True),
        (SuggestedPostAction.DECLINE, True, False),
    ],
)
def test_missing_fresh_right_fails_before_native_mutation(
    action: SuggestedPostAction,
    can_post: bool,
    can_manage: bool,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot(
                    can_post_messages=can_post,
                    can_manage_direct_messages=can_manage,
                )
                _, _, candidate, _ = await _seed(session, bot)
                bot.approve_calls.clear()
                bot.decline_calls.clear()
                with pytest.raises(SuggestedPostActionError) as raised:
                    await SuggestedPostActionService(session, bot=bot).execute(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                        action=action,
                    )
                assert raised.value.failure is SuggestedPostActionFailure.INSUFFICIENT_RIGHTS
                assert bot.get_member_calls[-1] == (PARENT_CHAT_ID, BOT_USER_ID)
                assert bot.approve_calls == []
                assert bot.decline_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_parent_mismatch_fails_before_rights_or_native_mutation() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot()
                _, _, candidate, _ = await _seed(session, bot)
                bot.parent_chat_id = -1007999
                bot.get_member_calls.clear()
                bot.approve_calls.clear()
                with pytest.raises(SuggestedPostActionError) as raised:
                    await SuggestedPostActionService(session, bot=bot).execute(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                        action=SuggestedPostAction.APPROVE,
                    )
                assert raised.value.failure is SuggestedPostActionFailure.ROUTING_MISMATCH
                assert bot.get_member_calls == []
                assert bot.approve_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_action_request_rejects_client_attempts_to_override_server_identity() -> None:
    with pytest.raises(ValidationError):
        SuggestedPostActionRequest.model_validate(
            {
                "action": "approve",
                "channel_id": 999,
                "connector_id": 888,
                "direct_messages_chat_id": -100123,
                "message_id": 456,
                "native_state": "approved",
            }
        )


def test_terminal_and_duplicate_actions_converge_without_second_native_mutation() -> None:
    async def run(action: SuggestedPostAction) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot()
                _, _, candidate, _ = await _seed(session, bot)
                service = SuggestedPostActionService(session, bot=bot)
                first = await service.execute(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    action=action,
                    comment="No" if action is SuggestedPostAction.DECLINE else None,
                )
                second = await service.execute(
                    candidate_id=int(candidate.id),
                    actor_client_id=1,
                    action=action,
                    comment="No" if action is SuggestedPostAction.DECLINE else None,
                )
                assert first.reused_existing is False
                assert second.reused_existing is True
                assert len(bot.approve_calls) + len(bot.decline_calls) == 1

                opposite = (
                    SuggestedPostAction.DECLINE
                    if action is SuggestedPostAction.APPROVE
                    else SuggestedPostAction.APPROVE
                )
                with pytest.raises(SuggestedPostActionError) as raised:
                    await service.execute(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                        action=opposite,
                    )
                assert raised.value.failure is SuggestedPostActionFailure.NOT_ACTIONABLE
                assert len(bot.approve_calls) + len(bot.decline_calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run(SuggestedPostAction.APPROVE))
    asyncio.run(run(SuggestedPostAction.DECLINE))


def test_approve_decline_race_has_one_native_winner_and_one_terminal_recheck(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'suggested-actions.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            bot = FakeActionBot(action_delay=0.02)
            async with Session() as seed_session:
                _, _, candidate, _ = await _seed(seed_session, bot)
                candidate_id = int(candidate.id)

            async def call(action: SuggestedPostAction):
                async with Session() as session:
                    return await SuggestedPostActionService(session, bot=bot).execute(
                        candidate_id=candidate_id,
                        actor_client_id=1,
                        action=action,
                    )

            results = await asyncio.gather(
                call(SuggestedPostAction.APPROVE),
                call(SuggestedPostAction.DECLINE),
                return_exceptions=True,
            )
            assert len(bot.approve_calls) + len(bot.decline_calls) == 1
            assert sum(not isinstance(result, Exception) for result in results) == 1
            failures = [result for result in results if isinstance(result, SuggestedPostActionError)]
            assert len(failures) == 1
            assert failures[0].failure is SuggestedPostActionFailure.NOT_ACTIONABLE

            async with Session() as verify_session:
                _, document = await _rows(verify_session)
                final_event = document.meta["telegram_suggested_post_lifecycle"]["event"]
                assert final_event in {"approved", "declined"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_transient_telegram_failure_never_marks_local_terminal_success() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot()
                _, _, candidate, _ = await _seed(session, bot)
                bot.approve_calls.clear()
                bot.approve_error = TelegramNetworkError(
                    method=ApproveSuggestedPost(chat_id=DM_CHAT_ID, message_id=77),
                    message="network unavailable",
                )
                with pytest.raises(SuggestedPostActionError) as raised:
                    await SuggestedPostActionService(session, bot=bot).execute(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                        action=SuggestedPostAction.APPROVE,
                    )
                assert raised.value.failure is SuggestedPostActionFailure.TRANSIENT_FAILURE
                _, document = await _rows(session)
                assert "telegram_suggested_post_lifecycle" not in dict(document.meta or {})
                assert document.meta["telegram_suggested_post_info"]["state"] == "pending"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_later_native_lifecycle_update_converges_same_source_and_candidate_identity() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot()
                _, _, candidate, document = await _seed(session, bot)
                candidate_id = int(candidate.id)
                document_id = int(document.id)
                await SuggestedPostActionService(session, bot=bot).execute(
                    candidate_id=candidate_id,
                    actor_client_id=1,
                    action=SuggestedPostAction.APPROVE,
                )

                lifecycle = await TelegramSuggestedPostIngestionService(
                    session, bot=bot
                ).ingest(_approved_service(_proposal()))
                assert lifecycle.reconciliation is not None
                assert int(lifecycle.reconciliation.candidate.id) == candidate_id
                assert int(lifecycle.reconciliation.document.id) == document_id
                assert lifecycle.reconciliation.document.meta[
                    "telegram_suggested_post_lifecycle"
                ]["event"] == "approved"
                assert lifecycle.reconciliation.document.content == "Initial Suggested Post"
                assert lifecycle.reconciliation.candidate.meta["rewrite_run_id"] == 4242
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_malformed_provenance_fails_closed_without_native_mutation() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = FakeActionBot()
                _, _, candidate, document = await _seed(session, bot)
                metadata = dict(document.meta or {})
                metadata["telegram_message_id"] = 0
                document.meta = metadata
                await session.commit()
                bot.approve_calls.clear()

                with pytest.raises(SuggestedPostActionError) as raised:
                    await SuggestedPostActionService(session, bot=bot).execute(
                        candidate_id=int(candidate.id),
                        actor_client_id=1,
                        action=SuggestedPostAction.APPROVE,
                    )
                assert raised.value.failure is SuggestedPostActionFailure.MALFORMED_PROVENANCE
                assert bot.approve_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
