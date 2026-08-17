from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from aiogram.methods import SavePreparedKeyboardButton
from aiogram.types import ChatAdministratorRights, KeyboardButton, KeyboardButtonRequestChat
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.studio_channel_onboarding import StudioChannelOnboardingRequest

_REQUEST_ID_MIN = 1_000_000
_REQUEST_ID_MAX = 2_147_483_647
_DEFAULT_TTL = timedelta(minutes=15)
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})


class PreparedChannelButtonProvider(Protocol):
    async def prepare(self, *, tg_user_id: int, request_id: int) -> str: ...


@dataclass(frozen=True, slots=True)
class PreparedChannelOnboarding:
    request_id: int
    prepared_button_id: str
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ChannelOnboardingRequestView:
    request_id: int
    status: str
    expires_at: datetime
    selected_chat_id: int | None
    channel_id: int | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class ChannelOnboardingClaim:
    ok: bool
    reason: str
    request_id: int
    client_id: int | None = None
    expected_tg_user_id: int | None = None


class ChannelOnboardingPrepareError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _channel_rights(*, can_promote_members: bool) -> ChatAdministratorRights:
    return ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=False,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=can_promote_members,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_post_messages=True,
        can_edit_messages=True,
    )


class AiogramPreparedChannelButtonProvider:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def prepare(self, *, tg_user_id: int, request_id: int) -> str:
        button = KeyboardButton(
            text="Выбрать канал",
            request_chat=KeyboardButtonRequestChat(
                request_id=request_id,
                chat_is_channel=True,
                user_administrator_rights=_channel_rights(can_promote_members=True),
                bot_administrator_rights=_channel_rights(can_promote_members=False),
                bot_is_member=True,
                request_title=True,
                request_username=True,
            ),
        )
        prepared = await self._bot(
            SavePreparedKeyboardButton(user_id=tg_user_id, button=button)
        )
        prepared_id = str(getattr(prepared, "id", "") or "").strip()
        if not prepared_id:
            raise ChannelOnboardingPrepareError("Telegram returned an empty prepared button id")
        return prepared_id


class StudioChannelOnboardingRequestService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provider: PreparedChannelButtonProvider,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._ttl = ttl

    async def _reserve(
        self,
        *,
        client_id: int,
        tg_user_id: int,
    ) -> StudioChannelOnboardingRequest:
        for _ in range(20):
            request_id = _REQUEST_ID_MIN + secrets.randbelow(
                _REQUEST_ID_MAX - _REQUEST_ID_MIN + 1
            )
            async with self._session_factory() as session:
                row = StudioChannelOnboardingRequest(
                    request_id=request_id,
                    client_id=client_id,
                    expected_tg_user_id=tg_user_id,
                    status="reserved",
                    expires_at=_now() + self._ttl,
                )
                session.add(row)
                try:
                    await session.commit()
                    await session.refresh(row)
                    return row
                except IntegrityError:
                    await session.rollback()
        raise ChannelOnboardingPrepareError("Unable to reserve a unique onboarding request id")

    async def prepare(
        self,
        *,
        client_id: int,
        tg_user_id: int,
    ) -> PreparedChannelOnboarding:
        row = await self._reserve(client_id=client_id, tg_user_id=tg_user_id)
        try:
            prepared_button_id = await self._provider.prepare(
                tg_user_id=tg_user_id,
                request_id=int(row.request_id),
            )
        except Exception as exc:
            async with self._session_factory() as session:
                await session.execute(
                    update(StudioChannelOnboardingRequest)
                    .where(
                        StudioChannelOnboardingRequest.id == row.id,
                        StudioChannelOnboardingRequest.status == "reserved",
                    )
                    .values(
                        status="failed",
                        failure_reason="prepare-provider-error",
                    )
                )
                await session.commit()
            if isinstance(exc, ChannelOnboardingPrepareError):
                raise
            raise ChannelOnboardingPrepareError(
                "Telegram could not prepare channel picker"
            ) from exc

        async with self._session_factory() as session:
            transition = await session.execute(
                update(StudioChannelOnboardingRequest)
                .where(
                    StudioChannelOnboardingRequest.id == row.id,
                    StudioChannelOnboardingRequest.status == "reserved",
                )
                .values(
                    prepared_button_id=prepared_button_id,
                    status="prepared",
                )
            )
            await session.commit()
            if transition.rowcount != 1:
                raise ChannelOnboardingPrepareError(
                    "Onboarding request lost its reservation"
                )
            current = await session.get(StudioChannelOnboardingRequest, row.id)
            if current is None:
                raise ChannelOnboardingPrepareError("Onboarding request disappeared")
            return PreparedChannelOnboarding(
                request_id=int(current.request_id),
                prepared_button_id=prepared_button_id,
                status=current.status,
                expires_at=_aware_utc(current.expires_at),
            )

    async def get_for_client(
        self,
        *,
        client_id: int,
        request_id: int,
    ) -> ChannelOnboardingRequestView | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(StudioChannelOnboardingRequest).where(
                        StudioChannelOnboardingRequest.request_id == request_id,
                        StudioChannelOnboardingRequest.client_id == client_id,
                    )
                )
            ).scalars().first()
            if row is None:
                return None
            if row.status == "prepared" and _aware_utc(row.expires_at) <= _now():
                await session.execute(
                    update(StudioChannelOnboardingRequest)
                    .where(
                        StudioChannelOnboardingRequest.id == row.id,
                        StudioChannelOnboardingRequest.status == "prepared",
                    )
                    .values(status="expired", failure_reason="expired")
                )
                await session.commit()
                await session.refresh(row)
            return self._view(row)

    async def cancel_for_client(
        self,
        *,
        client_id: int,
        request_id: int,
    ) -> ChannelOnboardingRequestView | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(StudioChannelOnboardingRequest).where(
                        StudioChannelOnboardingRequest.request_id == request_id,
                        StudioChannelOnboardingRequest.client_id == client_id,
                    )
                )
            ).scalars().first()
            if row is None:
                return None
            await session.execute(
                update(StudioChannelOnboardingRequest)
                .where(
                    StudioChannelOnboardingRequest.id == row.id,
                    StudioChannelOnboardingRequest.status.in_(("reserved", "prepared")),
                )
                .values(status="cancelled", failure_reason="cancelled")
            )
            await session.commit()
            await session.refresh(row)
            return self._view(row)

    async def claim_shared(
        self,
        *,
        request_id: int,
        sender_tg_user_id: int,
        selected_chat_id: int,
    ) -> ChannelOnboardingClaim:
        now = _now()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(StudioChannelOnboardingRequest).where(
                        StudioChannelOnboardingRequest.request_id == request_id
                    )
                )
            ).scalars().first()
            if row is None:
                return ChannelOnboardingClaim(False, "not-found", request_id)
            if int(row.expected_tg_user_id) != int(sender_tg_user_id):
                return ChannelOnboardingClaim(False, "sender-mismatch", request_id)
            if row.status != "prepared":
                reason = "terminal" if row.status in _TERMINAL_STATUSES else "not-prepared"
                return ChannelOnboardingClaim(False, reason, request_id)
            if _aware_utc(row.expires_at) <= now:
                expiry = await session.execute(
                    update(StudioChannelOnboardingRequest)
                    .where(
                        StudioChannelOnboardingRequest.id == row.id,
                        StudioChannelOnboardingRequest.status == "prepared",
                    )
                    .values(status="expired", failure_reason="expired")
                )
                await session.commit()
                if expiry.rowcount == 1:
                    return ChannelOnboardingClaim(False, "expired", request_id)
                return ChannelOnboardingClaim(False, "not-prepared", request_id)

            claim = await session.execute(
                update(StudioChannelOnboardingRequest)
                .where(
                    StudioChannelOnboardingRequest.id == row.id,
                    StudioChannelOnboardingRequest.status == "prepared",
                    StudioChannelOnboardingRequest.expected_tg_user_id
                    == sender_tg_user_id,
                    StudioChannelOnboardingRequest.expires_at > now,
                )
                .values(
                    status="processing",
                    selected_chat_id=int(selected_chat_id),
                )
            )
            await session.commit()
            if claim.rowcount != 1:
                await session.refresh(row)
                if row.status in _TERMINAL_STATUSES:
                    return ChannelOnboardingClaim(False, "terminal", request_id)
                return ChannelOnboardingClaim(False, "not-prepared", request_id)

            return ChannelOnboardingClaim(
                True,
                "claimed",
                request_id,
                client_id=int(row.client_id),
                expected_tg_user_id=int(row.expected_tg_user_id),
            )

    async def complete(
        self,
        *,
        request_id: int,
        succeeded: bool,
        channel_id: int | None,
        failure_reason: str | None,
    ) -> bool:
        async with self._session_factory() as session:
            completion = await session.execute(
                update(StudioChannelOnboardingRequest)
                .where(
                    StudioChannelOnboardingRequest.request_id == request_id,
                    StudioChannelOnboardingRequest.status == "processing",
                )
                .values(
                    status="succeeded" if succeeded else "failed",
                    channel_id=(
                        int(channel_id)
                        if succeeded and channel_id is not None
                        else None
                    ),
                    failure_reason=(
                        None
                        if succeeded
                        else (failure_reason or "onboarding-failed")
                    ),
                )
            )
            await session.commit()
            return completion.rowcount == 1

    @staticmethod
    def _view(row: StudioChannelOnboardingRequest) -> ChannelOnboardingRequestView:
        return ChannelOnboardingRequestView(
            request_id=int(row.request_id),
            status=str(row.status),
            expires_at=_aware_utc(row.expires_at),
            selected_chat_id=(
                int(row.selected_chat_id) if row.selected_chat_id is not None else None
            ),
            channel_id=int(row.channel_id) if row.channel_id is not None else None,
            failure_reason=row.failure_reason,
        )
