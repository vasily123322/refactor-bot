from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.models import Channel, Client
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.channel_onboarding import ChannelOnboardingService


class FakeTelegram:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(type="channel", title="Verified channel")
        self.bot = SimpleNamespace(id=999)
        self.member = SimpleNamespace(
            status="administrator",
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
        )

    async def get_me(self):
        return self.bot

    async def get_chat(self, chat_id: int):
        return self.chat

    async def get_chat_member(self, chat_id: int, user_id: int):
        return self.member


async def _fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _onboard(factory, *, user_id: int = 101, chat_id: int = -1001):
    return await ChannelOnboardingService(
        session_factory=factory,
        telegram=FakeTelegram(),
    ).onboard_channel(
        requester_tg_user_id=user_id,
        requester_username=f"user{user_id}",
        requester_full_name=f"User {user_id}",
        chat_id=chat_id,
    )


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("unique race"))


def test_concurrent_client_unique_winner_is_reused(monkeypatch) -> None:
    async def run() -> None:
        engine, factory = await _fixture()
        try:
            async def raced_create_or_get(self, tg_user_id, username, full_name):
                winner = Client(
                    tg_user_id=tg_user_id,
                    username=username,
                    full_name=full_name,
                )
                self.session.add(winner)
                await self.session.commit()
                raise _integrity_error()

            monkeypatch.setattr(ClientsRepo, "create_or_get", raced_create_or_get)
            result = await _onboard(factory)
            assert result.ok is True
            assert result.created is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_same_owner_channel_winner_is_idempotent(monkeypatch) -> None:
    async def run() -> None:
        engine, factory = await _fixture()
        try:
            async def raced_create(self, owner_id, tg_chat_id, title):
                winner = Channel(
                    owner_id=owner_id,
                    tg_chat_id=tg_chat_id,
                    title=title,
                    is_active=True,
                )
                self.session.add(winner)
                await self.session.commit()
                raise _integrity_error()

            monkeypatch.setattr(ChannelsRepo, "create", raced_create)
            result = await _onboard(factory)
            assert result.ok is True
            assert result.created is False
            assert result.reason == "connected"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_different_owner_channel_winner_is_conflict(monkeypatch) -> None:
    async def run() -> None:
        engine, factory = await _fixture()
        try:
            async def raced_create(self, owner_id, tg_chat_id, title):
                other = Client(
                    tg_user_id=909,
                    username="other",
                    full_name="Other owner",
                )
                self.session.add(other)
                await self.session.flush()
                winner = Channel(
                    owner_id=int(other.id),
                    tg_chat_id=tg_chat_id,
                    title=title,
                    is_active=True,
                )
                self.session.add(winner)
                await self.session.commit()
                raise _integrity_error()

            monkeypatch.setattr(ChannelsRepo, "create", raced_create)
            result = await _onboard(factory)
            assert result.ok is False
            assert result.reason == "owner-conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())
