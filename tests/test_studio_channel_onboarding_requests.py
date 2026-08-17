from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.models import Client
from app.domain.studio_channel_onboarding import StudioChannelOnboardingRequest
from app.services.studio_channel_onboarding_requests import (
    AiogramPreparedChannelButtonProvider,
    ChannelOnboardingPrepareError,
    StudioChannelOnboardingRequestService,
)


class FakeProvider:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, int]] = []

    async def prepare(self, *, tg_user_id: int, request_id: int) -> str:
        self.calls.append((tg_user_id, request_id))
        if self.error is not None:
            raise self.error
        return f"prepared-{request_id}"


class RecordingBot:
    def __init__(self) -> None:
        self.method = None

    async def __call__(self, method):
        self.method = method
        return SimpleNamespace(id="prepared-native-id")


async def _fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        client = Client(tg_user_id=7001, username="owner", full_name="Owner")
        other = Client(tg_user_id=7002, username="other", full_name="Other")
        session.add_all([client, other])
        await session.commit()
        await session.refresh(client)
        await session.refresh(other)
        return engine, factory, int(client.id), int(other.id)


def test_aiogram_provider_preserves_native_request_correlation_and_rights() -> None:
    async def run() -> None:
        bot = RecordingBot()
        provider = AiogramPreparedChannelButtonProvider(bot)
        result = await provider.prepare(tg_user_id=7001, request_id=1_234_567)
        assert result == "prepared-native-id"
        method = bot.method
        assert method is not None
        assert method.user_id == 7001
        request_chat = method.button.request_chat
        assert request_chat is not None
        assert request_chat.request_id == 1_234_567
        assert request_chat.chat_is_channel is True
        assert request_chat.bot_is_member is True
        assert request_chat.bot_administrator_rights.can_post_messages is True
        assert request_chat.bot_administrator_rights.can_edit_messages is True
        assert request_chat.bot_administrator_rights.can_delete_messages is True
        assert request_chat.user_administrator_rights.can_post_messages is True
        assert request_chat.user_administrator_rights.can_edit_messages is True
        assert request_chat.user_administrator_rights.can_delete_messages is True

    asyncio.run(run())


def test_prepare_success_is_durable_and_client_scoped() -> None:
    async def run() -> None:
        engine, factory, client_id, other_id = await _fixture()
        try:
            provider = FakeProvider()
            service = StudioChannelOnboardingRequestService(
                session_factory=factory,
                provider=provider,
            )
            prepared = await service.prepare(client_id=client_id, tg_user_id=7001)
            assert 1_000_000 <= prepared.request_id <= 2_147_483_647
            assert prepared.status == "prepared"
            assert prepared.prepared_button_id == f"prepared-{prepared.request_id}"
            assert provider.calls == [(7001, prepared.request_id)]

            own = await service.get_for_client(
                client_id=client_id,
                request_id=prepared.request_id,
            )
            assert own is not None and own.status == "prepared"
            assert (
                await service.get_for_client(
                    client_id=other_id,
                    request_id=prepared.request_id,
                )
                is None
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_failure_marks_request_failed() -> None:
    async def run() -> None:
        engine, factory, client_id, _ = await _fixture()
        try:
            service = StudioChannelOnboardingRequestService(
                session_factory=factory,
                provider=FakeProvider(error=RuntimeError("provider down")),
            )
            try:
                await service.prepare(client_id=client_id, tg_user_id=7001)
            except ChannelOnboardingPrepareError:
                pass
            else:
                raise AssertionError("prepare must fail")

            async with factory() as session:
                row = (
                    await session.execute(select(StudioChannelOnboardingRequest))
                ).scalar_one()
                assert row.status == "failed"
                assert row.failure_reason == "prepare-provider-error"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sender_mismatch_does_not_consume_request() -> None:
    async def run() -> None:
        engine, factory, client_id, _ = await _fixture()
        try:
            service = StudioChannelOnboardingRequestService(
                session_factory=factory,
                provider=FakeProvider(),
            )
            prepared = await service.prepare(client_id=client_id, tg_user_id=7001)
            mismatch = await service.claim_shared(
                request_id=prepared.request_id,
                sender_tg_user_id=9999,
                selected_chat_id=-10055,
            )
            assert mismatch.ok is False
            assert mismatch.reason == "sender-mismatch"
            view = await service.get_for_client(
                client_id=client_id,
                request_id=prepared.request_id,
            )
            assert view is not None and view.status == "prepared"
            assert view.selected_chat_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_first_claim_wins_and_completion_is_terminal_no_replay() -> None:
    async def run() -> None:
        engine, factory, client_id, _ = await _fixture()
        try:
            service = StudioChannelOnboardingRequestService(
                session_factory=factory,
                provider=FakeProvider(),
            )
            prepared = await service.prepare(client_id=client_id, tg_user_id=7001)
            first = await service.claim_shared(
                request_id=prepared.request_id,
                sender_tg_user_id=7001,
                selected_chat_id=-10077,
            )
            assert first.ok is True
            assert first.client_id == client_id

            duplicate = await service.claim_shared(
                request_id=prepared.request_id,
                sender_tg_user_id=7001,
                selected_chat_id=-10088,
            )
            assert duplicate.ok is False
            assert duplicate.reason == "not-prepared"

            assert await service.complete(
                request_id=prepared.request_id,
                succeeded=True,
                channel_id=42,
                failure_reason=None,
            )
            assert not await service.complete(
                request_id=prepared.request_id,
                succeeded=True,
                channel_id=43,
                failure_reason=None,
            )
            view = await service.get_for_client(
                client_id=client_id,
                request_id=prepared.request_id,
            )
            assert view is not None
            assert view.status == "succeeded"
            assert view.selected_chat_id == -10077
            assert view.channel_id == 42
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cancel_and_expiry_prevent_late_shared_chat_authorization() -> None:
    async def run() -> None:
        engine, factory, client_id, _ = await _fixture()
        try:
            service = StudioChannelOnboardingRequestService(
                session_factory=factory,
                provider=FakeProvider(),
            )
            cancelled = await service.prepare(client_id=client_id, tg_user_id=7001)
            cancelled_view = await service.cancel_for_client(
                client_id=client_id,
                request_id=cancelled.request_id,
            )
            assert cancelled_view is not None and cancelled_view.status == "cancelled"
            claim = await service.claim_shared(
                request_id=cancelled.request_id,
                sender_tg_user_id=7001,
                selected_chat_id=-10090,
            )
            assert claim.ok is False and claim.reason == "terminal"

            expiring = StudioChannelOnboardingRequestService(
                session_factory=factory,
                provider=FakeProvider(),
                ttl=timedelta(seconds=-1),
            )
            expired = await expiring.prepare(client_id=client_id, tg_user_id=7001)
            claim = await expiring.claim_shared(
                request_id=expired.request_id,
                sender_tg_user_id=7001,
                selected_chat_id=-10091,
            )
            assert claim.ok is False and claim.reason == "expired"
            view = await expiring.get_for_client(
                client_id=client_id,
                request_id=expired.request_id,
            )
            assert view is not None and view.status == "expired"
        finally:
            await engine.dispose()

    asyncio.run(run())
