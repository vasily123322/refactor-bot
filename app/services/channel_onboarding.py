from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import Channel, Client
from app.repositories.admin import BansRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo

_REQUIRED_CHANNEL_RIGHTS = (
    ("can_post_messages", "post"),
    ("can_edit_messages", "edit"),
    ("can_delete_messages", "delete"),
)


class ChannelOnboardingTelegramGateway(Protocol):
    async def get_me(self) -> Any: ...

    async def get_chat(self, chat_id: int) -> Any: ...

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any: ...


@dataclass(frozen=True, slots=True)
class ChannelOnboardingResult:
    ok: bool
    reason: str
    channel_id: int | None = None
    created: bool = False
    title: str | None = None
    missing_rights: tuple[str, ...] = ()


def _member_status(member: Any) -> str:
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status) or "")


def _missing_channel_rights(member: Any) -> tuple[str, ...] | None:
    status = _member_status(member)
    if status in {"creator", "owner"}:
        return ()
    if status != "administrator":
        return None
    return tuple(
        label
        for attribute, label in _REQUIRED_CHANNEL_RIGHTS
        if not bool(getattr(member, attribute, False))
    )


async def _reconcile_existing_channel(
    session: AsyncSession,
    *,
    existing: Channel,
    requester_client_id: int,
    title: str | None,
) -> ChannelOnboardingResult:
    """Return/reconcile an already durable channel without ever changing its owner."""

    if int(existing.owner_id) != int(requester_client_id):
        await session.rollback()
        return ChannelOnboardingResult(ok=False, reason="owner-conflict")

    changed = False
    if title and existing.title != title:
        existing.title = title
        changed = True
    if not existing.is_active:
        existing.is_active = True
        changed = True
    if changed:
        await session.commit()
        await session.refresh(existing)
    return ChannelOnboardingResult(
        ok=True,
        reason="connected",
        channel_id=int(existing.id),
        created=False,
        title=existing.title,
    )


class ChannelOnboardingService:
    """Verify Telegram authority before persisting a Studio/legacy channel owner."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        telegram: ChannelOnboardingTelegramGateway,
    ) -> None:
        self._session_factory = session_factory
        self._telegram = telegram

    async def onboard_channel(
        self,
        *,
        requester_tg_user_id: int,
        requester_username: str | None,
        requester_full_name: str | None,
        chat_id: int,
    ) -> ChannelOnboardingResult:
        if requester_tg_user_id <= 0 or chat_id == 0:
            return ChannelOnboardingResult(ok=False, reason="invalid-input")

        async with self._session_factory() as session:
            if await BansRepo(session).is_banned(chat_id):
                return ChannelOnboardingResult(ok=False, reason="banned")

        try:
            chat = await self._telegram.get_chat(chat_id)
            if str(
                getattr(
                    getattr(chat, "type", ""),
                    "value",
                    getattr(chat, "type", ""),
                )
            ) != "channel":
                return ChannelOnboardingResult(ok=False, reason="not-channel")

            bot_user = await self._telegram.get_me()
            bot_member = await self._telegram.get_chat_member(chat_id, int(bot_user.id))
            requester_member = await self._telegram.get_chat_member(
                chat_id,
                requester_tg_user_id,
            )
        except TelegramForbiddenError:
            return ChannelOnboardingResult(ok=False, reason="bot-not-present")
        except Exception:
            return ChannelOnboardingResult(ok=False, reason="telegram-unavailable")

        bot_missing = _missing_channel_rights(bot_member)
        if bot_missing is None:
            return ChannelOnboardingResult(ok=False, reason="bot-not-admin")
        if bot_missing:
            return ChannelOnboardingResult(
                ok=False,
                reason="bot-missing-rights",
                missing_rights=bot_missing,
            )

        requester_missing = _missing_channel_rights(requester_member)
        if requester_missing is None:
            return ChannelOnboardingResult(ok=False, reason="requester-not-admin")
        if requester_missing:
            return ChannelOnboardingResult(
                ok=False,
                reason="requester-missing-rights",
                missing_rights=requester_missing,
            )

        title = getattr(chat, "title", None) or getattr(chat, "full_name", None)
        title = str(title) if title else None

        async with self._session_factory() as session:
            clients = ClientsRepo(session)
            try:
                client = await clients.create_or_get(
                    requester_tg_user_id,
                    requester_username,
                    requester_full_name,
                )
            except IntegrityError:
                # A concurrent first request for the same Telegram user may win the
                # globally unique Client.tg_user_id insert. Re-read that durable winner
                # instead of leaking an IntegrityError out of the authority boundary.
                await session.rollback()
                client = (
                    await session.execute(
                        select(Client).where(
                            Client.tg_user_id == int(requester_tg_user_id)
                        )
                    )
                ).scalars().first()
                if client is None:
                    return ChannelOnboardingResult(
                        ok=False,
                        reason="persistence-conflict",
                    )

            requester_client_id = int(client.id)
            existing = (
                await session.execute(
                    select(Channel).where(Channel.tg_chat_id == chat_id)
                )
            ).scalars().first()

            if existing is not None:
                return await _reconcile_existing_channel(
                    session,
                    existing=existing,
                    requester_client_id=requester_client_id,
                    title=title,
                )

            try:
                channel = await ChannelsRepo(session).create(
                    owner_id=requester_client_id,
                    tg_chat_id=chat_id,
                    title=title,
                )
            except IntegrityError:
                # Channel.tg_chat_id is globally unique. If another verified onboarding
                # wins after our SELECT, resolve the committed winner deterministically:
                # same owner is an idempotent reconnect; another owner is a hard conflict.
                await session.rollback()
                existing = (
                    await session.execute(
                        select(Channel).where(Channel.tg_chat_id == chat_id)
                    )
                ).scalars().first()
                if existing is None:
                    return ChannelOnboardingResult(
                        ok=False,
                        reason="persistence-conflict",
                    )
                return await _reconcile_existing_channel(
                    session,
                    existing=existing,
                    requester_client_id=requester_client_id,
                    title=title,
                )

            return ChannelOnboardingResult(
                ok=True,
                reason="connected",
                channel_id=int(channel.id),
                created=True,
                title=channel.title,
            )
