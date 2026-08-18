from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
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


class FakeBot:
    def __init__(self) -> None:
        self.approve_calls: list[tuple[int, int]] = []

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(
            id=int(chat_id),
            is_direct_messages=True,
            parent_chat=SimpleNamespace(id=PARENT_CHAT_ID),
        )

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(can_post_messages=True, can_manage_direct_messages=True)

    async def approve_suggested_post(self, *, chat_id: int, message_id: int) -> bool:
        self.approve_calls.append((int(chat_id), int(message_id)))
        return True

    async def decline_suggested_post(
        self, *, chat_id: int, message_id: int, comment: str | None = None
    ) -> bool:
        return True


def _proposal() -> Message:
    return Message.model_validate(
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


def test_unknown_persisted_price_fails_before_native_approve() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                channel = Channel(owner_id=1, tg_chat_id=PARENT_CHAT_ID, title="Canonical")
                session.add(channel)
                await session.flush()
                await SourcesRepo(session).create_connector(
                    channel_id=int(channel.id),
                    kind=SUGGESTED_POST_CONNECTOR_KIND,
                    value=str(PARENT_CHAT_ID),
                    mode="rewrite",
                )
                bot = FakeBot()
                ingested = await TelegramSuggestedPostIngestionService(
                    session, bot=bot
                ).ingest(_proposal())
                assert ingested.reconciliation is not None
                document = ingested.reconciliation.document
                metadata = dict(document.meta or {})
                info = dict(metadata["telegram_suggested_post_info"])
                info["price"] = {"currency": "FUTURE", "amount": 77}
                metadata["telegram_suggested_post_info"] = info
                document.meta = metadata
                await session.commit()

                with pytest.raises(SuggestedPostActionError) as raised:
                    await SuggestedPostActionService(session, bot=bot).execute(
                        candidate_id=int(ingested.reconciliation.candidate.id),
                        actor_client_id=1,
                        action=SuggestedPostAction.APPROVE,
                    )
                assert raised.value.failure is SuggestedPostActionFailure.INVALID_REQUEST
                assert bot.approve_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())
