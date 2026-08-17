from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.models import BannedChat, Channel, Client
from app.services.channel_onboarding import ChannelOnboardingService


class FakeTelegram:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(type="channel", title="Verified channel")
        self.bot_user = SimpleNamespace(id=999)
        self.bot_member = self.admin()
        self.requester_member = self.admin()
        self.chat_error: Exception | None = None
        self.member_error: Exception | None = None

    @staticmethod
    def admin(**overrides):
        values = {
            "status": "administrator",
            "can_post_messages": True,
            "can_edit_messages": True,
            "can_delete_messages": True,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def owner():
        return SimpleNamespace(status="creator")

    async def get_me(self):
        return self.bot_user

    async def get_chat(self, chat_id: int):
        if self.chat_error is not None:
            raise self.chat_error
        return self.chat

    async def get_chat_member(self, chat_id: int, user_id: int):
        if self.member_error is not None:
            raise self.member_error
        if user_id == self.bot_user.id:
            return self.bot_member
        return self.requester_member


async def _new_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _onboard(
    factory,
    telegram: FakeTelegram,
    *,
    user_id: int = 101,
    chat_id: int = -1001,
):
    return await ChannelOnboardingService(
        session_factory=factory,
        telegram=telegram,
    ).onboard_channel(
        requester_tg_user_id=user_id,
        requester_username=f"user{user_id}",
        requester_full_name=f"User {user_id}",
        chat_id=chat_id,
    )


def test_valid_channel_creates_owner_bound_channel() -> None:
    async def run() -> None:
        engine, factory = await _new_fixture()
        try:
            telegram = FakeTelegram()
            result = await _onboard(factory, telegram)
            assert result.ok is True
            assert result.created is True

            async with factory() as session:
                channel = (await session.execute(select(Channel))).scalar_one()
                client = (await session.execute(select(Client))).scalar_one()
                assert channel.owner_id == client.id
                assert client.tg_user_id == 101
                assert channel.tg_chat_id == -1001
                assert channel.title == "Verified channel"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_non_channel_and_banned_channel_fail_closed() -> None:
    async def run() -> None:
        engine, factory = await _new_fixture()
        try:
            telegram = FakeTelegram()
            telegram.chat = SimpleNamespace(type="supergroup", title="Not a channel")
            result = await _onboard(factory, telegram)
            assert result.reason == "not-channel"

            async with factory() as session:
                assert (await session.execute(select(Channel))).scalars().all() == []
                session.add(BannedChat(tg_chat_id=-1002, reason="blocked", created_by=None))
                await session.commit()

            telegram = FakeTelegram()
            telegram.chat_error = AssertionError("banned chat must not reach Telegram")
            result = await _onboard(factory, telegram, chat_id=-1002)
            assert result.reason == "banned"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_forbidden_bot_access_rejected_without_persistence() -> None:
    async def run() -> None:
        engine, factory = await _new_fixture()
        try:
            telegram = FakeTelegram()
            telegram.member_error = TelegramForbiddenError(
                method=object(),
                message="Forbidden",
            )
            result = await _onboard(factory, telegram)
            assert result.reason == "bot-not-present"
            async with factory() as session:
                assert (await session.execute(select(Channel))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_bot_and_requester_must_be_admin_with_post_edit_delete_rights() -> None:
    async def run() -> None:
        engine, factory = await _new_fixture()
        try:
            telegram = FakeTelegram()
            telegram.bot_member = SimpleNamespace(status="member")
            result = await _onboard(factory, telegram)
            assert result.reason == "bot-not-admin"

            telegram.bot_member = telegram.admin(
                can_edit_messages=False,
                can_delete_messages=False,
            )
            result = await _onboard(factory, telegram, chat_id=-1002)
            assert result.reason == "bot-missing-rights"
            assert result.missing_rights == ("edit", "delete")

            telegram.bot_member = telegram.admin()
            telegram.requester_member = SimpleNamespace(status="member")
            result = await _onboard(factory, telegram, chat_id=-1003)
            assert result.reason == "requester-not-admin"

            telegram.requester_member = telegram.admin(can_post_messages=False)
            result = await _onboard(factory, telegram, chat_id=-1004)
            assert result.reason == "requester-missing-rights"
            assert result.missing_rights == ("post",)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_creator_status_satisfies_required_channel_authority() -> None:
    async def run() -> None:
        engine, factory = await _new_fixture()
        try:
            telegram = FakeTelegram()
            telegram.bot_member = telegram.owner()
            telegram.requester_member = telegram.owner()
            result = await _onboard(factory, telegram)
            assert result.ok is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_existing_different_owner_is_conflict_never_reassigned() -> None:
    async def run() -> None:
        engine, factory = await _new_fixture()
        try:
            telegram = FakeTelegram()
            first = await _onboard(factory, telegram, user_id=101)
            assert first.ok is True

            result = await _onboard(factory, telegram, user_id=202)
            assert result.ok is False
            assert result.reason == "owner-conflict"

            async with factory() as session:
                channel = (await session.execute(select(Channel))).scalar_one()
                owner = await session.get(Client, channel.owner_id)
                assert owner is not None
                assert owner.tg_user_id == 101
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_same_owner_reconnect_refreshes_title_and_reactivates() -> None:
    async def run() -> None:
        engine, factory = await _new_fixture()
        try:
            telegram = FakeTelegram()
            first = await _onboard(factory, telegram, user_id=101)
            assert first.ok is True

            async with factory() as session:
                channel = (await session.execute(select(Channel))).scalar_one()
                original_owner_id = channel.owner_id
                channel.is_active = False
                await session.commit()

            telegram.chat = SimpleNamespace(type="channel", title="Renamed channel")
            second = await _onboard(factory, telegram, user_id=101)
            assert second.ok is True
            assert second.created is False
            assert second.title == "Renamed channel"

            async with factory() as session:
                channel = (await session.execute(select(Channel))).scalar_one()
                owner = await session.get(Client, channel.owner_id)
                assert channel.owner_id == original_owner_id
                assert owner is not None and owner.tg_user_id == 101
                assert channel.title == "Renamed channel"
                assert channel.is_active is True
        finally:
            await engine.dispose()

    asyncio.run(run())
