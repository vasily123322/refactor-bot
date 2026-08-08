from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.scheduling import as_utc
from app.services.source_ingestion import SourceIngestionError
from app.services.telegram_source_ingestion import TelegramSourceIngestionService
from app.userbot.client import UserbotChat, UserbotMessage


class _Gateway:
    def __init__(self, *, username: str | None = "source_channel", fail: bool = False) -> None:
        self.chat = UserbotChat(id=-1001234567890, username=username, title="Source Channel")
        self.fail = fail
        self.joined: list[str | int] = []

    async def get_chat(self, target: str | int) -> UserbotChat:
        if self.fail:
            raise RuntimeError("not authorized secret target")
        return self.chat

    async def join_chat(self, target: str | int):
        self.joined.append(target)
        if self.fail:
            raise RuntimeError("invite rejected secret target")
        return self.chat

    async def get_chat_history(self, target: str | int, *, limit: int = 100):
        assert limit == 100
        yield UserbotMessage(
            id=11,
            chat=self.chat,
            text="First Telegram post",
            date=datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc),
        )
        yield UserbotMessage(
            id=12,
            chat=self.chat,
            text="Second Telegram post",
            date=datetime(2026, 8, 8, 9, 0),
        )
        yield UserbotMessage(id=13, chat=self.chat, text=None, caption=None)


def test_telegram_ingestion_is_idempotent_and_creates_public_message_links() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=61,
                    kind="telegram",
                    value="@source_channel",
                    mode="summary",
                    reuse_policy="reference_only",
                )
                service = TelegramSourceIngestionService(session, gateway=_Gateway())
                first = await service.ingest(connector)
                second = await service.ingest(connector)

                assert first.documents_seen == 2
                assert first.documents_created == 2
                assert first.candidates_created == 2
                assert second.documents_seen == 2
                assert second.documents_created == 0
                assert second.candidates_created == 0

                documents = (
                    await session.execute(
                        select(SourceDocument).order_by(SourceDocument.external_id)
                    )
                ).scalars().all()
                candidates = (
                    await session.execute(
                        select(ContentCandidate).order_by(ContentCandidate.id)
                    )
                ).scalars().all()
                assert len(documents) == 2
                assert len(candidates) == 2
                assert documents[0].external_id == "telegram:-1001234567890:11"
                assert documents[0].source_url == "https://t.me/source_channel/11"
                assert documents[0].title == "Source Channel"
                assert documents[1].published_at is not None
                assert as_utc(documents[1].published_at).utcoffset().total_seconds() == 0
                assert all(row.suggested_action == "summarize" for row in candidates)
                assert connector.auth_state == "ready"
                assert connector.status == "healthy"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_private_telegram_source_does_not_invent_public_message_url() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=62,
                    kind="telegram",
                    value="-1001234567890",
                )
                await TelegramSourceIngestionService(
                    session, gateway=_Gateway(username=None)
                ).ingest(connector)
                documents = (
                    await session.execute(select(SourceDocument))
                ).scalars().all()
                assert len(documents) == 2
                assert all(row.source_url is None for row in documents)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_inaccessible_telegram_source_marks_auth_required_without_leaking_target() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=63,
                    kind="telegram",
                    value="https://t.me/+private-invite-secret",
                )
                with pytest.raises(SourceIngestionError, match="not accessible"):
                    await TelegramSourceIngestionService(
                        session, gateway=_Gateway(fail=True)
                    ).ingest(connector)
                assert connector.status == "auth_required"
                assert connector.auth_state == "session_required"
                assert "private-invite-secret" not in str(connector.status_reason)
                assert (
                    await session.execute(select(SourceDocument))
                ).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())
