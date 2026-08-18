from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import SourceConnector
from app.repositories.channels import ChannelsRepo
from app.repositories.sources_v2 import SourcesRepo


class ChannelDMBot(Protocol):
    async def get_chat(self, chat_id: int): ...


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
    """Resolve trusted routing for Telegram Channel Direct Messages only."""

    def __init__(self, session: AsyncSession, *, bot: ChannelDMBot) -> None:
        self.bot = bot
        self.channels = ChannelsRepo(session)
        self.sources = SourcesRepo(session)

    async def resolve(
        self,
        *,
        direct_messages_chat_id: int,
        connector_kind: str,
    ) -> ChannelDMContext:
        chat = await self.bot.get_chat(int(direct_messages_chat_id))
        if getattr(chat, "is_direct_messages", None) is False:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.DIRECT_MESSAGES_CHAT_REQUIRED
            )

        parent_chat = getattr(chat, "parent_chat", None)
        parent_chat_id = int(getattr(parent_chat, "id", 0) or 0)
        if parent_chat_id == 0 or getattr(parent_chat, "type", "channel") != "channel":
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.PARENT_CHANNEL_REQUIRED
            )

        channel = await self.channels.get_by_chat_id(parent_chat_id)
        if channel is None:
            raise ChannelDMContextRoutingError(ChannelDMContextErrorCode.CHANNEL_NOT_FOUND)

        connectors = [
            connector
            for connector in await self.sources.list_connectors(int(channel.id))
            if connector.enabled
            and str(connector.kind) == str(connector_kind)
            and str(connector.value) == str(parent_chat_id)
        ]
        if not connectors:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.CONNECTOR_NOT_CONFIGURED
            )
        if len(connectors) != 1:
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.CONNECTOR_AMBIGUOUS
            )

        connector = connectors[0]
        if int(connector.channel_id) != int(channel.id):
            raise ChannelDMContextRoutingError(
                ChannelDMContextErrorCode.CONNECTOR_CHANNEL_MISMATCH
            )
        return ChannelDMContext(
            direct_messages_chat_id=int(direct_messages_chat_id),
            parent_chat_id=parent_chat_id,
            channel_id=int(connector.channel_id),
            connector=connector,
        )
