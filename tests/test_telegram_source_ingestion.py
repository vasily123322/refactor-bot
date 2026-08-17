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
from app.services.telegram_source_ingestion import (
    TELEGRAM_CURSOR_KEY,
    TelegramSourceIngestionService,
    telegram_backlog_hint,
    telegram_cursor_message_id,
)
from app.userbot.client import UserbotChat, UserbotMessage


class _Gateway:
    def __init__(
        self,
        *,
        username: str | None = "source_channel",
        fail: bool = False,
        messages: list[UserbotMessage] | None = None,
    ) -> None:
        self.chat = UserbotChat(id=-1001234567890, username=username, title="Source Channel")
        self.fail = fail
        self.joined: list[str | int] = []
        self.calls: list[tuple[int, bool, int]] = []
        self.messages = messages or [
            UserbotMessage(
                id=12,
                chat=self.chat,
                text="Second Telegram post",
                date=datetime(2026, 8, 8, 9, 0),
            ),
            UserbotMessage(
                id=11,
                chat=self.chat,
                text="First Telegram post",
                date=datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc),
            ),
            UserbotMessage(id=13, chat=self.chat, text=None, caption=None),
        ]
        for message in self.messages:
            message.chat = self.chat

    async def get_chat(self, target: str | int) -> UserbotChat:
        if self.fail:
            raise RuntimeError("not authorized secret target")
        return self.chat

    async def join_chat(self, target: str | int):
        self.joined.append(target)
        if self.fail:
            raise RuntimeError("invite rejected secret target")
        return self.chat

    async def get_chat_history(
        self,
        target: str | int,
        *,
        limit: int = 100,
        min_id: int = 0,
        reverse: bool = False,
    ):
        self.calls.append((int(min_id), bool(reverse), int(limit)))
        rows = [message for message in self.messages if int(message.id) > int(min_id)]
        rows.sort(key=lambda message: int(message.id), reverse=not reverse)
        for message in rows[:limit]:
            yield message


def test_telegram_ingestion_bootstraps_cursor_then_becomes_healthy_noop() -> None:
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
                gateway = _Gateway()
                service = TelegramSourceIngestionService(session, gateway=gateway)
                first = await service.ingest(connector)
                assert telegram_cursor_message_id(connector) == 13
                assert telegram_backlog_hint(connector) is False

                second = await service.ingest(connector)
                assert first.documents_seen == 2
                assert first.documents_created == 2
                assert first.candidates_created == 2
                assert second.documents_seen == 0
                assert second.documents_created == 0
                assert second.candidates_created == 0
                assert gateway.calls == [(0, False, 100), (13, True, 100)]
                assert telegram_cursor_message_id(connector) == 13
                assert telegram_backlog_hint(connector) is False

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
                assert connector.last_document_at is not None
                assert as_utc(connector.last_document_at) == datetime(
                    2026, 8, 8, 9, 0, tzinfo=timezone.utc
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_incremental_cursor_drains_backlog_without_skipping_over_page_limit() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            chat = UserbotChat(id=-1001234567890, username="source_channel", title="Source")
            messages = [
                UserbotMessage(id=message_id, chat=chat, text=f"Post {message_id}")
                for message_id in range(101, 251)
            ]
            gateway = _Gateway(messages=messages)
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=64,
                    kind="telegram",
                    value="@source_channel",
                    config={TELEGRAM_CURSOR_KEY: 100},
                )
                service = TelegramSourceIngestionService(
                    session,
                    gateway=gateway,
                    history_limit=100,
                )

                first = await service.ingest(connector)
                assert first.documents_created == 100
                assert telegram_cursor_message_id(connector) == 200
                assert telegram_backlog_hint(connector) is True

                second = await service.ingest(connector)
                assert second.documents_created == 50
                assert telegram_cursor_message_id(connector) == 250
                assert telegram_backlog_hint(connector) is False

                third = await service.ingest(connector)
                assert third.documents_seen == 0
                assert third.documents_created == 0
                assert telegram_backlog_hint(connector) is False
                assert gateway.calls == [
                    (100, True, 100),
                    (200, True, 100),
                    (250, True, 100),
                ]

                documents = (
                    await session.execute(
                        select(SourceDocument).order_by(SourceDocument.external_id)
                    )
                ).scalars().all()
                candidates = (
                    await session.execute(select(ContentCandidate))
                ).scalars().all()
                assert len(documents) == 150
                assert len(candidates) == 150
                message_ids = {int(row.meta["telegram_message_id"]) for row in documents}
                assert message_ids == set(range(101, 251))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_candidate_failure_rolls_back_document_without_advancing_cursor() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            chat = UserbotChat(id=-1001234567890, username="source_channel", title="Source")
            gateway = _Gateway(
                messages=[
                    UserbotMessage(id=101, chat=chat, text="Post 101"),
                    UserbotMessage(id=102, chat=chat, text="Post 102"),
                ]
            )
            async with Session() as session:
                connector = await SourcesRepo(session).create_connector(
                    channel_id=65,
                    kind="telegram",
                    value="@source_channel",
                    config={TELEGRAM_CURSOR_KEY: 100},
                )
                first_service = TelegramSourceIngestionService(session, gateway=gateway)
                original_add = first_service.reconciler.repo.add_candidate
                failed_once = False

                async def fail_candidate(row: ContentCandidate) -> ContentCandidate:
                    nonlocal failed_once
                    if not failed_once:
                        failed_once = True
                        raise RuntimeError("candidate persistence interrupted")
                    return await original_add(row)

                first_service.reconciler.repo.add_candidate = fail_candidate  # type: ignore[method-assign]
                with pytest.raises(SourceIngestionError, match="history read failed"):
                    await first_service.ingest(connector)

                assert telegram_cursor_message_id(connector) == 100
                documents_after_failure = (
                    await session.execute(select(SourceDocument))
                ).scalars().all()
                candidates_after_failure = (
                    await session.execute(select(ContentCandidate))
                ).scalars().all()
                assert documents_after_failure == []
                assert candidates_after_failure == []

                retry = await TelegramSourceIngestionService(session, gateway=gateway).ingest(
                    connector
                )
                assert retry.documents_created == 2
                assert retry.candidates_created == 2
                assert telegram_cursor_message_id(connector) == 102
                assert telegram_backlog_hint(connector) is False
                documents = (
                    await session.execute(select(SourceDocument))
                ).scalars().all()
                candidates = (
                    await session.execute(select(ContentCandidate))
                ).scalars().all()
                assert len(documents) == 2
                assert len(candidates) == 2
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
