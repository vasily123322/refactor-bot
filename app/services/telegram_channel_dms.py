from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.source_reconciliation import (
    SourceIngestionReconciliationService,
    SourceProjection,
    SourceProjectionUpdateMode,
    SourceReconciliationResult,
)
from app.services.telegram_channel_dm_context import (
    ChannelDMContextResolver,
    ChannelDMContextRoutingError,
)


TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND = "telegram_channel_dms"

_SUGGESTED_POST_FIELDS = (
    "suggested_post_info",
    "suggested_post_approval_failed",
    "suggested_post_approved",
    "suggested_post_declined",
    "suggested_post_paid",
    "suggested_post_refunded",
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


class TelegramChannelDMBot(Protocol):
    async def get_chat(self, chat_id: int): ...


class TelegramChannelDMRoutingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramChannelDMResult:
    reconciliation: SourceReconciliationResult


def channel_dm_external_id(chat_id: int, message_id: int) -> str:
    return f"dm:{int(chat_id)}:{int(message_id)}"


def _dump(value: Any) -> Any:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", exclude_none=True)
    return value


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


def _media_metadata(message: Message) -> dict[str, Any]:
    media: dict[str, Any] = {}
    for field in _MEDIA_FIELDS:
        value = getattr(message, field, None)
        if value is not None:
            media[field] = _dump(value)
    return media


def is_ordinary_channel_dm(message: Message) -> bool:
    if not bool(getattr(message.chat, "is_direct_messages", False)):
        return False
    if any(getattr(message, field, None) is not None for field in _SUGGESTED_POST_FIELDS):
        return False
    topic = message.direct_messages_topic
    user = message.from_user
    if topic is None or topic.user is None or user is None:
        return False
    if user.is_bot or message.sender_chat is not None:
        return False
    if int(user.id) != int(topic.user.id):
        return False
    return bool(_content(message))


class TelegramChannelDMIngestionService:
    """Project ordinary human Channel DMs into canonical source reconciliation."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        bot: TelegramChannelDMBot,
    ) -> None:
        self.context = ChannelDMContextResolver(session, bot=bot)
        self.reconciler = SourceIngestionReconciliationService(session)

    async def ingest(self, message: Message) -> TelegramChannelDMResult | None:
        if not is_ordinary_channel_dm(message):
            return None

        dm_chat_id = int(message.chat.id)
        try:
            context = await self.context.resolve(
                direct_messages_chat_id=dm_chat_id,
                connector_kind=TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
            )
        except ChannelDMContextRoutingError as exc:
            raise TelegramChannelDMRoutingError(exc.code.value) from exc

        topic = message.direct_messages_topic
        assert topic is not None and topic.user is not None and message.from_user is not None

        metadata: dict[str, Any] = {
            "transport": TELEGRAM_CHANNEL_DMS_CONNECTOR_KIND,
            "telegram_direct_messages_chat_id": dm_chat_id,
            "telegram_message_id": int(message.message_id),
            "telegram_parent_chat_id": int(context.parent_chat_id),
            "telegram_direct_messages_topic": _dump(topic),
            "telegram_sender": {
                "user": {
                    "id": int(message.from_user.id),
                    "is_bot": bool(message.from_user.is_bot),
                    "username": message.from_user.username,
                    "first_name": message.from_user.first_name,
                    "last_name": message.from_user.last_name,
                }
            },
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
        if message.reply_to_message is not None:
            metadata["telegram_reply_to"] = {
                "chat_id": int(message.reply_to_message.chat.id),
                "message_id": int(message.reply_to_message.message_id),
            }
        if message.media_group_id is not None:
            metadata["telegram_media_group_id"] = str(message.media_group_id)
        if message.edit_date is not None:
            metadata["telegram_edit_date"] = message.edit_date.isoformat()

        result = await self.reconciler.reconcile(
            context.connector,
            SourceProjection(
                external_id=channel_dm_external_id(dm_chat_id, int(message.message_id)),
                content=_content(message),
                author=_author(message),
                published_at=message.date,
                metadata=metadata,
                update_mode=SourceProjectionUpdateMode.CONTENT,
            ),
        )
        return TelegramChannelDMResult(result)
