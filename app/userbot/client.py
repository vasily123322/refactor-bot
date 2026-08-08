from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import unquote, urlparse

from loguru import logger
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

from app.core.config import settings
from app.userbot.session_migration import normalize_userbot_session


@dataclass(slots=True)
class UserbotChat:
    id: int
    username: str | None = None
    title: str | None = None


@dataclass(slots=True)
class UserbotMember:
    status: str


@dataclass(slots=True)
class UserbotMessage:
    id: int
    chat: UserbotChat
    text: str | None = None
    caption: str | None = None
    date: datetime | None = None
    _reply: Callable[[str], Awaitable[Any]] | None = None

    async def reply_text(self, text: str):
        if self._reply is None:
            raise RuntimeError("reply is unavailable for this message")
        return await self._reply(text)


def _build_proxy() -> dict[str, Any] | None:
    """Build Telethon/python-socks proxy configuration from env settings."""
    if settings.userbot_proxy_url:
        parsed = urlparse(settings.userbot_proxy_url)
        if not parsed.scheme or not parsed.hostname or not parsed.port:
            raise ValueError(
                "USERBOT_PROXY_URL must look like socks5://host:port or http://user:pass@host:port"
            )
        scheme = parsed.scheme.lower()
        if scheme == "https":
            # python-socks treats HTTPS CONNECT proxies as HTTP proxy endpoints;
            # TLS is used by Telegram after the CONNECT tunnel is established.
            scheme = "http"
        proxy: dict[str, Any] = {
            "proxy_type": scheme,
            "addr": parsed.hostname,
            "port": int(parsed.port),
            "rdns": True,
        }
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    if settings.userbot_proxy_host and settings.userbot_proxy_port:
        scheme = (settings.userbot_proxy_scheme or "socks5").lower()
        if scheme == "https":
            scheme = "http"
        proxy = {
            "proxy_type": scheme,
            "addr": settings.userbot_proxy_host,
            "port": int(settings.userbot_proxy_port),
            "rdns": True,
        }
        if settings.userbot_proxy_username:
            proxy["username"] = settings.userbot_proxy_username
        if settings.userbot_proxy_password:
            proxy["password"] = settings.userbot_proxy_password
        return proxy
    return None


def _invite_hash(target: str) -> str | None:
    value = target.strip()
    if "t.me/+" in value:
        return value.split("t.me/+", 1)[1].split("?", 1)[0].strip("/") or None
    if "t.me/joinchat/" in value:
        return value.split("t.me/joinchat/", 1)[1].split("?", 1)[0].strip("/") or None
    return None


def _public_target(target: str | int) -> str | int:
    if isinstance(target, int):
        return target
    value = target.strip()
    if value.startswith("@"):
        return value[1:]
    for marker in ("t.me/", "telegram.me/"):
        if marker in value:
            path = value.split(marker, 1)[1].split("?", 1)[0].strip("/")
            if path and not path.startswith("+") and not path.startswith("joinchat/"):
                return path.split("/", 1)[0]
    return value


class UserbotGateway:
    """Small compatibility boundary around Telethon for source-reading features."""

    def __init__(self) -> None:
        normalized, converted = normalize_userbot_session(
            settings.userbot_session,
            configured_api_id=settings.api_id,
        )
        if converted:
            logger.info("Userbot: legacy Pyrogram session converted to Telethon in memory")

        session = StringSession(normalized) if normalized else "userbot"
        self._client = TelegramClient(
            session,
            settings.api_id,
            settings.api_hash,
            proxy=_build_proxy(),
        )

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.disconnect()

    async def get_me(self):
        return await self._client.get_me()

    async def get_dialogs(self, limit: int = 200) -> AsyncIterator[Any]:
        async for dialog in self._client.iter_dialogs(limit=limit):
            yield dialog

    async def join_chat(self, target: str | int):
        if isinstance(target, str):
            invite = _invite_hash(target)
            if invite:
                try:
                    result = await self._client(ImportChatInviteRequest(invite))
                    chats = list(getattr(result, "chats", None) or [])
                    return self._adapt_chat(chats[0]) if chats else None
                except Exception as exc:
                    # Already joined is a normal path; CheckChatInvite below is
                    # authoritative and avoids string-matching RPC exception names.
                    logger.debug("Userbot private join did not import invite: {!r}", exc)
                    checked = await self._client(CheckChatInviteRequest(invite))
                    chat = getattr(checked, "chat", None)
                    if chat is not None:
                        return self._adapt_chat(chat)
                    raise

        entity = await self._client.get_entity(_public_target(target))
        try:
            await self._client(JoinChannelRequest(entity))
        except Exception as exc:
            name = type(exc).__name__
            if name not in {"UserAlreadyParticipantError", "ChannelPrivateError"}:
                raise
        return self._adapt_chat(entity)

    async def get_chat(self, target: str | int) -> UserbotChat:
        if isinstance(target, str):
            invite = _invite_hash(target)
            if invite:
                checked = await self._client(CheckChatInviteRequest(invite))
                chat = getattr(checked, "chat", None)
                if chat is None:
                    raise ValueError("private invite is not joined")
                return self._adapt_chat(chat)
        entity = await self._client.get_entity(_public_target(target))
        return self._adapt_chat(entity)

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> UserbotMember:
        channel = await self._client.get_input_entity(_public_target(chat_id))
        participant = await self._client.get_input_entity(int(user_id))
        result = await self._client(
            GetParticipantRequest(channel=channel, participant=participant)
        )
        cls = type(getattr(result, "participant", None)).__name__.lower()
        if "creator" in cls:
            status = "creator"
        elif "admin" in cls:
            status = "administrator"
        elif "left" in cls or "banned" in cls:
            status = "left"
        else:
            status = "member"
        return UserbotMember(status=status)

    async def get_chat_history(
        self, target: str | int, *, limit: int = 100
    ) -> AsyncIterator[UserbotMessage]:
        entity = await self._client.get_entity(_public_target(target))
        chat = self._adapt_chat(entity)
        async for message in self._client.iter_messages(entity, limit=limit):
            yield self._adapt_message(message, chat=chat)

    def on_command(self, commands: list[str] | tuple[str, ...]):
        wanted = {str(command).lower().lstrip("/") for command in commands}

        def decorator(handler):
            async def callback(event):
                text = (getattr(event, "raw_text", None) or "").strip()
                command = text.split(maxsplit=1)[0].lstrip("/").split("@", 1)[0].lower()
                if command not in wanted:
                    return
                chat_entity = await event.get_chat()
                message = self._adapt_message(
                    event.message,
                    chat=self._adapt_chat(chat_entity),
                    reply=event.reply,
                )
                await handler(self, message)

            self._client.add_event_handler(callback, events.NewMessage(incoming=True))
            return handler

        return decorator

    def on_channel_post(self):
        def decorator(handler):
            async def callback(event):
                chat_entity = await event.get_chat()
                # Pyrogram filters.channel matched broadcast channels, not
                # megagroups; preserve that behavior during the migration.
                if not bool(getattr(chat_entity, "broadcast", False)):
                    return
                message = self._adapt_message(
                    event.message,
                    chat=self._adapt_chat(chat_entity),
                    reply=event.reply,
                )
                await handler(self, message)

            self._client.add_event_handler(callback, events.NewMessage(incoming=True))
            return handler

        return decorator

    @staticmethod
    def _adapt_chat(entity: Any) -> UserbotChat:
        return UserbotChat(
            id=int(utils.get_peer_id(entity)),
            username=getattr(entity, "username", None),
            title=getattr(entity, "title", None),
        )

    @staticmethod
    def _adapt_message(
        message: Any,
        *,
        chat: UserbotChat,
        reply: Callable[[str], Awaitable[Any]] | None = None,
    ) -> UserbotMessage:
        raw = (
            getattr(message, "raw_text", None)
            or getattr(message, "message", None)
            or ""
        )
        # Telethon exposes text and media captions through the same `message`
        # field. Exposing it through both compatibility fields keeps existing
        # source-processing semantics intact.
        return UserbotMessage(
            id=int(getattr(message, "id", 0) or 0),
            chat=chat,
            text=raw or None,
            caption=raw or None,
            date=getattr(message, "date", None),
            _reply=reply,
        )


app = UserbotGateway()
