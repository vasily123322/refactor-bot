from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources import SourceConnector
from app.infrastructure.repositories.channels import ChannelsRepo
from app.infrastructure.repositories.sources_v2 import SourcesRepository


class ChannelDMContextErrorCode(str, Enum):
    DIRECT_MESSAGES_CHAT_REQUIRED = "DIRECT_MESSAGES_CHAT_REQUIRED"
    PARENT_CHANNEL_REQUIRED = "DIRECT_MESSAGES_PARENT_CHANNEL_REQUIRED"
    CHANNEL_NOT_FOUND = "CHANNEL_DM_CHANNEL_NOT_FOUND"
    CONNECTOR_NOT_CONFIGURED = "CHANNEL_DM_CONNECTOR_NOT_CONFIGURED"
    CONNECTOR_AMBIGUOUS = "CHANNEL_DM_CONNECTOR_AMBIGUOUS"
    CONNECTOR_CHANNEL_MISMATCH = "CHANNEL_DM_CONNECTOR_CHANNEL_MISMATCH"


class ChannelDMContextRoutingError(RuntimeError):
    def __init__(self, code: ChannelDMContextErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ChannelDMContext:
    direct_messages_chat_id: int
    parent_chat_id: int
    channel_id: int
    connector: SourceConnector


class ChannelDMContextResolver:
    """Resolve trusted routing for Telegram Channel Direct Messages.

    This seam owns transport/routing verification only. Source reconciliation and
    feature policy stay with their respective ingestion services.
    """

    def __init__(self, session: AsyncSession, bot: Bot) -> None:
        self._bot = bot
        self._channels = ChannelsRepo(session)
        self._sources = SourcesRepository(session)

    async def resolve(
        self,
        *,
        direct_messages_chat_id: int,
        connector_kind: str,
    ) -> ChannelDMContext:
        direct_chat = await self._bot.get_chat(direct_messages_chat_id)
        if getattr(direct_chat, "is_direct_messages", None) is False:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.DIRECT_MESSAGES_CHAT_REQUIRED
            )

        parent_chat = getattr(direct_chat, "parent_chat", None)
        parent_chat_id = getattr(parent_chat, "id", None)
        if parent_chat_id is None:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.PARENT_CHANNEL_REQUIRED
            )
        if getattr(parent_chat, "type", "channel") != "channel":
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.PARENT_CHANNEL_REQUIRED
            )

        channel = await self._channels.get_by_chat_id(int(parent_chat_id))
        if channel is None:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.CHANNEL_NOT_FOUND
            )

        connectors = await self._sources.list_connectors(channel.id)
        matches = [
            connector
            for connector in connectors
            if connector.enabled
            and connector.kind == connector_kind
            and str(connector.value) == str(parent_chat_id)
        ]
        if not matches:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.CONNECTOR_NOT_CONFIGURED
            )
        if len(matches) != 1:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.CONNECTOR_AMBIGUOUS
            )

        connector = matches[0]
        if int(connector.channel_id) != int(channel.id):
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.CONNECTOR_CHANNEL_MISMATCH
            )

        return ChannelDMContext(
            direct_messages_chat_id=int(direct_messages_chat_id),
            parent_chat_id=int(parent_chat_id),
            channel_id=int(connector.channel_id),
            connector=connector,
        )
