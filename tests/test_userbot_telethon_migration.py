from __future__ import annotations

import asyncio
import base64
import struct
from types import SimpleNamespace

from telethon.sessions import StringSession

from app.services.posting import PostingService
from app.userbot.session_migration import (
    decode_pyrogram_session,
    normalize_userbot_session,
    pyrogram_to_telethon_session,
)


def _pyrogram_session(*, dc_id: int = 4, api_id: int = 123456) -> tuple[str, bytes]:
    auth_key = bytes((i % 251 for i in range(256)))
    packed = struct.pack(
        ">BI?256sQ?",
        dc_id,
        api_id,
        False,
        auth_key,
        987654321,
        False,
    )
    return base64.urlsafe_b64encode(packed).decode().rstrip("="), auth_key


def test_decode_current_pyrogram_session_format() -> None:
    value, auth_key = _pyrogram_session()
    dc_id, api_id, test_mode, decoded_key, user_id, is_bot = decode_pyrogram_session(value)
    assert (dc_id, api_id, test_mode, user_id, is_bot) == (
        4,
        123456,
        False,
        987654321,
        False,
    )
    assert decoded_key == auth_key


def test_pyrogram_session_converts_to_telethon_without_reauthorization() -> None:
    value, auth_key = _pyrogram_session(dc_id=4)
    converted = pyrogram_to_telethon_session(value, configured_api_id=123456)
    session = StringSession(converted)

    assert session.dc_id == 4
    assert session.server_address == "149.154.167.91"
    assert session.port == 443
    assert session.auth_key is not None
    assert session.auth_key.key == auth_key


def test_normalizer_leaves_telethon_session_unchanged() -> None:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    from telethon.crypto import AuthKey

    session.auth_key = AuthKey(b"x" * 256)
    value = session.save()
    normalized, converted = normalize_userbot_session(value, configured_api_id=123456)
    assert converted is False
    assert normalized == value


class _BotOnlyPublishing:
    def __init__(self) -> None:
        self.video_notes: list[dict] = []
        self.messages: list[dict] = []

    async def send_video_note(self, **kwargs):
        self.video_notes.append(kwargs)
        return SimpleNamespace(message_id=501)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=502)


def test_video_note_pair_uses_bot_api_only() -> None:
    async def run() -> None:
        bot = _BotOnlyPublishing()
        posting = PostingService(bot, lambda: None)  # type: ignore[arg-type]
        ids = await posting._dispatch(
            -100123,
            {
                "type": "video_note",
                "file_id": "telegram-file-id",
                "vn_pair": True,
                "caption": "caption",
                "buttons": [[{"text": "Open", "url": "https://example.com"}]],
                # Legacy payloads may still carry this field; it is ignored.
                "use_userbot": True,
            },
        )

        assert ids == [501]
        assert len(bot.video_notes) == 1
        assert bot.video_notes[0]["chat_id"] == -100123
        assert len(bot.messages) == 1
        assert bot.messages[0]["text"] == "caption"

    asyncio.run(run())
