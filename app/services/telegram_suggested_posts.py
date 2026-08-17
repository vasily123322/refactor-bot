from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sources.models import SourceConnector
from app.repositories.channels import ChannelsRepo
from app.repositories.sources_v2 import SourcesRepo
from app.services.source_reconciliation import (
    SourceIngestionReconciliationService,
    SourceProjection,
    SourceProjectionUpdateMode,
    SourceReconciliationResult,
)


SUGGESTED_POST_CONNECTOR_KIND = "telegram_suggested_posts"


class TelegramSuggestedPostError(RuntimeError):
    pass


class TelegramSuggestedPostRoutingError(TelegramSuggestedPostError):
    pass


class TelegramSuggestedPostDisposition(str, Enum):
    CONTENT = "content"
    LIFECYCLE = "lifecycle"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class TelegramSuggestedPostResult:
    disposition: TelegramSuggestedPostDisposition
    reconciliation: SourceReconciliationResult | None = None


class TelegramSuggestedPostBot(Protocol):
    async def get_chat(self, chat_id: int): ...


_LIFECYCLE_FIELDS = (
    ("suggested_post_approval_failed", "approval_failed"),
    ("suggested_post_approved", "approved"),
    ("suggested_post_declined", "declined"),
    ("suggested_post_paid", "paid"),
    ("suggested_post_refunded", "refunded"),
)

_MEDIA_FIELDS = (
    "animation",
    "audio",
    "document",
    "photo",
    "sticker",
    "video",
    "video_note",
    "voice",
)


def suggested_post_external_id(chat_id: int, message_id: int) -> str:
    return f"dm:{int(chat_id)}:{int(message_id)}"


def _dump(value: Any, *, exclude: set[str] | None = None) -> Any:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", exclude_none=True, exclude=exclude or set())
    return value


def _sender_metadata(message: Message) -> dict[str, Any]:
    sender: dict[str, Any] = {}
    if message.from_user is not None:
        sender["user"] = {
            "id": int(message.from_user.id),
            "is_bot": bool(message.from_user.is_bot),
            "username": message.from_user.username,
            "first_name": message.from_user.first_name,
            "last_name": message.from_user.last_name,
        }
    if message.sender_chat is not None:
        sender["sender_chat"] = {
            "id": int(message.sender_chat.id),
            "type": str(message.sender_chat.type),
            "title": message.sender_chat.title,
            "username": message.sender_chat.username,
        }
    return sender


def _media_metadata(message: Message) -> dict[str, Any]:
    media: dict[str, Any] = {}
    for field in _MEDIA_FIELDS:
        value = getattr(message, field, None)
        if value is not None:
            media[field] = _dump(value)
    return media


def _content(message: Message) -> str:
    text = (message.text or message.caption or "").strip()
    if text:
        return text
    for field in _MEDIA_FIELDS:
        if getattr(message, field, None) is not None:
            return f"[Telegram {field}]"
    return ""


def _author(message: Message) -> str | None:
    user = message.from_user
    if user is None:
        return None
    if user.username:
        return f"@{user.username}"
    full_name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    return full_name or str(int(user.id))


def _lifecycle_event(message: Message) -> tuple[str, Any] | None:
    for field, name in _LIFECYCLE_FIELDS:
        value = getattr(message, field, None)
        if value is not None:
            return name, value
    return None


class TelegramSuggestedPostIngestionService:
    """Normalize Telegram Suggested Posts into the canonical source reconciliation seam.

    Routing is accepted only when Telegram's DM parent channel agrees with the
    canonical Channel row and exactly one enabled Suggested Posts SourceConnector.
    The Telegram payload never supplies the internal channel id.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        bot: TelegramSuggestedPostBot,
    ) -> None:
        self.session = session
        self.bot = bot
        self.channels = ChannelsRepo(session)
        self.sources = SourcesRepo(session)
        self.reconciler = SourceIngestionReconciliationService(session)

    async def _resolve_connector(self, direct_messages_chat_id: int) -> tuple[SourceConnector, int]:
        chat = await self.bot.get_chat(int(direct_messages_chat_id))
        parent_chat = getattr(chat, "parent_chat", None)
        parent_chat_id = int(getattr(parent_chat, "id", 0) or 0)
        if parent_chat_id == 0:
            raise TelegramSuggestedPostRoutingError(
                "direct-messages chat has no verifiable parent channel"
            )

        channel = await self.channels.get_by_chat_id(parent_chat_id)
        if channel is None:
            raise TelegramSuggestedPostRoutingError(
                "direct-messages parent channel is not a canonical Channel"
            )

        connectors = await self.sources.list_connectors_by_kind_value(
            kind=SUGGESTED_POST_CONNECTOR_KIND,
            value=str(parent_chat_id),
            enabled_only=True,
        )
        connectors = [
            connector
            for connector in connectors
            if int(connector.channel_id) == int(channel.id)
        ]
        if len(connectors) != 1:
            if not connectors:
                raise TelegramSuggestedPostRoutingError(
                    "no trusted Suggested Posts connector matches the parent channel"
                )
            raise TelegramSuggestedPostRoutingError(
                "ambiguous trusted Suggested Posts connector mapping"
            )
        return connectors[0], parent_chat_id

    async def _reconcile_content(self, message: Message) -> TelegramSuggestedPostResult:
        topic = message.direct_messages_topic
        info = message.suggested_post_info
        if topic is None or info is None:
            return TelegramSuggestedPostResult(TelegramSuggestedPostDisposition.IGNORED)

        # Suggested Posts created by bots or channel identities are output/automation,
        # not an inbound user proposal. Human user identity is required here.
        if message.from_user is None or message.from_user.is_bot or message.sender_chat is not None:
            return TelegramSuggestedPostResult(TelegramSuggestedPostDisposition.IGNORED)

        content = _content(message)
        if not content:
            return TelegramSuggestedPostResult(TelegramSuggestedPostDisposition.IGNORED)

        dm_chat_id = int(message.chat.id)
        connector, parent_chat_id = await self._resolve_connector(dm_chat_id)
        metadata: dict[str, Any] = {
            "transport": "telegram_suggested_posts",
            "telegram_direct_messages_chat_id": dm_chat_id,
            "telegram_message_id": int(message.message_id),
            "telegram_parent_chat_id": parent_chat_id,
            "telegram_direct_messages_topic": _dump(topic),
            "telegram_suggested_post_info": _dump(info),
            "telegram_sender": _sender_metadata(message),
        }
        if message.entities:
            metadata["telegram_entities"] = [_dump(entity) for entity in message.entities]
        if message.caption_entities:
            metadata["telegram_caption_entities"] = [
                _dump(entity) for entity in message.caption_entities
            ]
        media = _media_metadata(message)
        if media:
            metadata["telegram_media"] = media

        result = await self.reconciler.reconcile(
            connector,
            SourceProjection(
                external_id=suggested_post_external_id(dm_chat_id, int(message.message_id)),
                content=content,
                author=_author(message),
                published_at=message.date,
                metadata=metadata,
                update_mode=SourceProjectionUpdateMode.CONTENT,
            ),
        )
        return TelegramSuggestedPostResult(
            TelegramSuggestedPostDisposition.CONTENT,
            result,
        )

    async def _reconcile_lifecycle(self, message: Message) -> TelegramSuggestedPostResult:
        lifecycle = _lifecycle_event(message)
        if lifecycle is None:
            return TelegramSuggestedPostResult(TelegramSuggestedPostDisposition.IGNORED)
        event_name, event = lifecycle
        original = getattr(event, "suggested_post_message", None)
        if original is None:
            # Telegram makes correlation optional on the wire. Without the native
            # original message identity there is no safe document to mutate.
            return TelegramSuggestedPostResult(TelegramSuggestedPostDisposition.IGNORED)

        dm_chat_id = int(original.chat.id)
        if int(message.chat.id) != dm_chat_id:
            raise TelegramSuggestedPostRoutingError(
                "lifecycle event chat disagrees with correlated Suggested Post chat"
            )
        connector, parent_chat_id = await self._resolve_connector(dm_chat_id)
        event_payload = _dump(event, exclude={"suggested_post_message"})
        metadata = {
            "transport": "telegram_suggested_posts",
            "telegram_direct_messages_chat_id": dm_chat_id,
            "telegram_message_id": int(original.message_id),
            "telegram_parent_chat_id": parent_chat_id,
            "telegram_suggested_post_lifecycle": {
                "event": event_name,
                "service_message_id": int(message.message_id),
                "payload": event_payload,
            },
        }
        result = await self.reconciler.reconcile(
            connector,
            SourceProjection(
                external_id=suggested_post_external_id(dm_chat_id, int(original.message_id)),
                metadata=metadata,
                update_mode=SourceProjectionUpdateMode.LIFECYCLE,
            ),
        )
        return TelegramSuggestedPostResult(
            TelegramSuggestedPostDisposition.LIFECYCLE,
            result,
        )

    async def ingest(self, message: Message) -> TelegramSuggestedPostResult:
        if message.suggested_post_info is not None:
            return await self._reconcile_content(message)
        return await self._reconcile_lifecycle(message)
