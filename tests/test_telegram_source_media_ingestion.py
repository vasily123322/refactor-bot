from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.sources.models import ContentCandidate, SourceDocument
from app.repositories.sources_v2 import SourcesRepo
from app.services.telegram_source_ingestion import TelegramSourceIngestionService
from app.userbot.client import UserbotChat, UserbotMedia, UserbotMessage


class _MediaGateway:
    def __init__(self, messages: list[UserbotMessage]) -> None:
        self.chat = UserbotChat(
            id=-100777000111,
            username="media_source",
            title="Media Source",
        )
        self.messages = messages
        for message in self.messages:
            message.chat = self.chat

    async def get_chat(self, target: str | int) -> UserbotChat:
        return self.chat

    async def join_chat(self, target: str | int):
        return self.chat

    async def get_chat_history(
        self,
        target: str | int,
        *,
        limit: int = 100,
        min_id: int = 0,
        reverse: bool = False,
    ):
        rows = [message for message in self.messages if int(message.id) > int(min_id)]
        rows.sort(key=lambda message: int(message.id), reverse=not reverse)
        for message in rows[:limit]:
            yield message


def test_media_only_telegram_message_survives_source_ingestion() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            media = UserbotMedia(
                kind="photo",
                mime_type="image/jpeg",
                size_bytes=345678,
                width=1600,
                height=900,
            )
            message = UserbotMessage(
                id=501,
                chat=UserbotChat(id=0),
                media=media,
                date=datetime(2026, 8, 9, 17, 30, tzinfo=timezone.utc),
            )
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=71,
                    kind="telegram",
                    value="@media_source",
                    mode="summary",
                )
                result = await TelegramSourceIngestionService(
                    session,
                    gateway=_MediaGateway([message]),
                ).ingest(connector)

                assert result.documents_seen == 1
                assert result.documents_created == 1
                assert result.candidates_created == 1
                document = (
                    await session.execute(select(SourceDocument))
                ).scalar_one()
                candidate = (
                    await session.execute(select(ContentCandidate))
                ).scalar_one()
                assert document.content == "[Telegram photo]"
                assert document.source_url == "https://t.me/media_source/501"
                assert document.meta["telegram_media"] == {
                    "kind": "photo",
                    "mime_type": "image/jpeg",
                    "size_bytes": 345678,
                    "width": 1600,
                    "height": 900,
                }
                serialized = repr(document.meta)
                assert "file_reference" not in serialized
                assert "access_hash" not in serialized
                assert candidate.suggested_action == "summarize"
                assert connector.status == "healthy"
                assert connector.auth_state == "ready"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_captioned_media_keeps_caption_as_content_and_media_metadata() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            message = UserbotMessage(
                id=502,
                chat=UserbotChat(id=0),
                caption="Launch clip caption",
                media=UserbotMedia(
                    kind="video",
                    mime_type="video/mp4",
                    duration_seconds=18,
                ),
            )
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=72,
                    kind="telegram",
                    value="@media_source",
                )
                await TelegramSourceIngestionService(
                    session,
                    gateway=_MediaGateway([message]),
                ).ingest(connector)
                document = (
                    await session.execute(select(SourceDocument))
                ).scalar_one()
                assert document.content == "Launch clip caption"
                assert document.meta["telegram_media"] == {
                    "kind": "video",
                    "mime_type": "video/mp4",
                    "duration_seconds": 18,
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_blank_non_media_message_remains_non_ingestible() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            message = UserbotMessage(id=503, chat=UserbotChat(id=0))
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=73,
                    kind="telegram",
                    value="@media_source",
                )
                result = await TelegramSourceIngestionService(
                    session,
                    gateway=_MediaGateway([message]),
                ).ingest(connector)
                assert result.documents_seen == 0
                assert (
                    await session.execute(select(SourceDocument))
                ).scalars().all() == []
                assert connector.status == "degraded"
                assert connector.status_reason == "Telegram source returned no ingestible messages"
        finally:
            await engine.dispose()

    asyncio.run(run())
