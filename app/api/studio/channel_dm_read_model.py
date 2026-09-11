from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class ChannelDMPersonView(BaseModel):
    id: int | None = None
    username: str | None = None
    display_name: str | None = None


class ChannelDMReplyView(BaseModel):
    chat_id: int | None = None
    message_id: int | None = None


class ChannelDMInboxView(BaseModel):
    transport: Literal["telegram_channel_dms"] = "telegram_channel_dms"
    sender: ChannelDMPersonView | None = None
    topic_user: ChannelDMPersonView | None = None
    topic_id: int | None = None
    received_at: datetime | None = None
    edited_at: datetime | None = None
    is_reply: bool = False
    reply_to: ChannelDMReplyView | None = None
    media_group_id: str | None = None
    direct_messages_chat_id: int | None = None
    message_id: int | None = None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    raw = _text(value)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _person(value: Any) -> ChannelDMPersonView | None:
    raw = _mapping(value)
    if raw is None:
        return None
    user_id = _integer(raw.get("id"))
    username = _text(raw.get("username"))
    first_name = _text(raw.get("first_name"))
    last_name = _text(raw.get("last_name"))
    if username:
        display_name = f"@{username.lstrip('@')}"
    else:
        display_name = " ".join(part for part in (first_name, last_name) if part) or None
    if user_id is None and username is None and display_name is None:
        return None
    return ChannelDMPersonView(
        id=user_id,
        username=username,
        display_name=display_name,
    )


def _reply(value: Any) -> ChannelDMReplyView | None:
    raw = _mapping(value)
    if raw is None:
        return None
    chat_id = _integer(raw.get("chat_id"))
    message_id = _integer(raw.get("message_id"))
    if chat_id is None and message_id is None:
        return None
    return ChannelDMReplyView(chat_id=chat_id, message_id=message_id)


def project_channel_dm_inbox(
    metadata: Mapping[str, Any] | None,
    *,
    received_at: datetime | None = None,
) -> ChannelDMInboxView | None:
    """Project persisted ordinary Channel-DM provenance into a typed read model.

    The exact persisted transport discriminator is the sole origin classifier. The
    browser receives selected presentation facts rather than raw SourceDocument.meta.
    Native identifiers are retained only for optional diagnostics and do not select
    candidate, Content, or Telegram reply authority.
    """

    raw = _mapping(metadata)
    if raw is None or raw.get("transport") != "telegram_channel_dms":
        return None

    sender = _mapping(raw.get("telegram_sender"))
    topic = _mapping(raw.get("telegram_direct_messages_topic"))
    reply = _reply(raw.get("telegram_reply_to"))

    return ChannelDMInboxView(
        sender=_person(sender.get("user") if sender is not None else None),
        topic_user=_person(topic.get("user") if topic is not None else None),
        topic_id=_integer(topic.get("topic_id")) if topic is not None else None,
        received_at=received_at,
        edited_at=_timestamp(raw.get("telegram_edit_date")),
        is_reply=reply is not None,
        reply_to=reply,
        media_group_id=_text(raw.get("telegram_media_group_id")),
        direct_messages_chat_id=_integer(raw.get("telegram_direct_messages_chat_id")),
        message_id=_integer(raw.get("telegram_message_id")),
    )
