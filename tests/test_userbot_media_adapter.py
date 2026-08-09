from __future__ import annotations

from types import SimpleNamespace

from app.userbot.client import UserbotChat, UserbotGateway


def _message(**kwargs):
    defaults = {
        "id": 51,
        "raw_text": "",
        "message": "",
        "date": None,
        "photo": None,
        "video_note": None,
        "gif": None,
        "voice": None,
        "video": None,
        "audio": None,
        "sticker": None,
        "document": None,
        "file": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_userbot_adapter_keeps_only_safe_photo_metadata() -> None:
    raw = _message(
        photo=object(),
        file=SimpleNamespace(
            mime_type="image/jpeg",
            size=123456,
            width=1280,
            height=720,
            duration=None,
            file_reference=b"private-reference",
            access_hash=999,
        ),
    )
    adapted = UserbotGateway._adapt_message(
        raw,
        chat=UserbotChat(id=-1001, username="source", title="Source"),
    )

    assert adapted.media is not None
    assert adapted.media.kind == "photo"
    assert adapted.media.to_metadata() == {
        "kind": "photo",
        "mime_type": "image/jpeg",
        "size_bytes": 123456,
        "width": 1280,
        "height": 720,
    }
    serialized = repr(adapted.media.to_metadata())
    assert "file_reference" not in serialized
    assert "access_hash" not in serialized
    assert "private-reference" not in serialized


def test_userbot_adapter_prefers_voice_note_over_generic_audio() -> None:
    adapted = UserbotGateway._adapt_message(
        _message(
            voice=object(),
            audio=object(),
            document=object(),
            file=SimpleNamespace(
                mime_type="audio/ogg",
                size=64000,
                width=None,
                height=None,
                duration=7,
            ),
        ),
        chat=UserbotChat(id=-1002),
    )

    assert adapted.media is not None
    assert adapted.media.kind == "voice_note"
    assert adapted.media.duration_seconds == 7


def test_userbot_adapter_leaves_plain_text_without_media_descriptor() -> None:
    adapted = UserbotGateway._adapt_message(
        _message(raw_text="hello", message="hello"),
        chat=UserbotChat(id=-1003),
    )
    assert adapted.text == "hello"
    assert adapted.media is None
