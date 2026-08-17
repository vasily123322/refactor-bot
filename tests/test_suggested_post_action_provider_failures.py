from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.methods import ApproveSuggestedPost
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel
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
USER = {"id": 501, "is_bot": False, "first_name": "Alice"}


class RejectingBot:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.mutation_calls = 0

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=PARENT_CHAT_ID),
        )

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(
            can_post_messages=True,
            can_manage_direct_messages=True,
        )

    async def approve_suggested_post(self, *, chat_id: int, message_id: int) -> bool:
        self.mutation_calls += 1
        raise self.error

    async def decline_suggested_post(
        self,
        *,
        chat_id: int,
        message_id: int,
        comment: str | None = None,
    ) -> bool:
        self.mutation_calls += 1
        raise self.error


async def _seed(session, bot: RejectingBot) -> int:
    channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical")
    session.add(channel)
    await session.flush()
    await SourcesRepo(session).create_connector(
        channel_id=int(channel.id),
        kind=SUGGESTED_POST_CONNECTOR_KIND,
        value=str(PARENT_CHAT_ID),
        mode="rewrite",
    )
    message = Message.model_validate(
        {
            "message_id": 77,
            "date": datetime(2026, 8, 18, tzinfo=timezone.utc),
            "chat": {"id": DM_CHAT_ID, "type": "private"},
            "from": USER,
            "text": "Proposal",
            "direct_messages_topic": {"topic_id": 123, "user": USER},
            "suggested_post_info": {"state": "pending"},
        }
    )
    result = await TelegramSuggestedPostIngestionService(session, bot=bot).ingest(message)
    assert result.reconciliation is not None
    return int(result.reconciliation.candidate.id)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            TelegramNotFound(
                method=ApproveSuggestedPost(chat_id=DM_CHAT_ID, message_id=77),
                message="message not found",
            ),
            SuggestedPostActionFailure.NATIVE_MISSING,
        ),
        (
            TelegramBadRequest(
                method=ApproveSuggestedPost(chat_id=DM_CHAT_ID, message_id=77),
                message="suggested post is no longer pending",
            ),
            SuggestedPostActionFailure.NOT_ACTIONABLE,
        ),
    ],
)
def test_provider_rejection_is_classified_and_does_not_become_local_success(
    error: Exception,
    expected: SuggestedPostActionFailure,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                bot = RejectingBot(error)
                candidate_id = await _seed(session, bot)
                bot.mutation_calls = 0
                with pytest.raises(SuggestedPostActionError) as raised:
                    await SuggestedPostActionService(session, bot=bot).execute(
                        candidate_id=candidate_id,
                        actor_client_id=1,
                        action=SuggestedPostAction.APPROVE,
                    )
                assert raised.value.failure is expected
                assert bot.mutation_calls == 1
                candidate = await session.get(
                    __import__(
                        "app.domain.sources.models",
                        fromlist=["ContentCandidate"],
                    ).ContentCandidate,
                    candidate_id,
                )
                assert candidate is not None
                document = await session.get(
                    __import__(
                        "app.domain.sources.models",
                        fromlist=["SourceDocument"],
                    ).SourceDocument,
                    int(candidate.source_document_id),
                )
                assert document is not None
                assert "telegram_suggested_post_lifecycle" not in dict(document.meta or {})
        finally:
            await engine.dispose()

    asyncio.run(run())
