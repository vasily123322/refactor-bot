from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.services.telegram_media_upload import (
    TelegramMediaUploadError,
    TelegramMediaUploadService,
)


@dataclass
class _Photo:
    file_id: str
    width: int
    height: int
    file_size: int


@dataclass
class _Video:
    file_id: str
    width: int
    height: int
    duration: int


@dataclass
class _Message:
    message_id: int
    photo: list[_Photo] | None = None
    video: _Video | None = None
    animation: object | None = None
    audio: object | None = None
    voice: object | None = None


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self.fail = False

    async def send_photo(self, *, chat_id: int, photo):
        self.sent.append(("photo", chat_id))
        if self.fail:
            raise RuntimeError("provider-secret-body")
        return _Message(
            message_id=11,
            photo=[
                _Photo("small", 100, 100, 1000),
                _Photo("large", 1200, 800, 5000),
            ],
        )

    async def send_video(self, *, chat_id: int, video):
        self.sent.append(("video", chat_id))
        return _Message(
            message_id=12,
            video=_Video("video-file-id", 1920, 1080, 42),
        )

    async def send_animation(self, *, chat_id: int, animation):
        raise AssertionError("not used")

    async def send_audio(self, *, chat_id: int, audio):
        raise AssertionError("not used")

    async def send_voice(self, *, chat_id: int, voice):
        raise AssertionError("not used")

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


def test_upload_stages_in_authenticated_user_chat_and_deletes_message() -> None:
    async def run() -> None:
        bot = _Bot()
        service = TelegramMediaUploadService(bot)  # type: ignore[arg-type]

        photo = await service.upload(
            tg_user_id=555,
            kind="photo",
            data=b"photo-bytes",
            filename="cover.jpg",
        )
        assert photo.telegram_file_id == "large"
        assert photo.width == 1200
        assert photo.height == 800
        assert bot.sent == [("photo", 555)]
        assert bot.deleted == [(555, 11)]

        video = await service.upload(
            tg_user_id=555,
            kind="video",
            data=b"video-bytes",
            filename="clip.mp4",
        )
        assert video.telegram_file_id == "video-file-id"
        assert video.duration_seconds == 42
        assert bot.sent[-1] == ("video", 555)
        assert bot.deleted[-1] == (555, 12)

    asyncio.run(run())


def test_upload_redacts_provider_failure_details() -> None:
    async def run() -> None:
        bot = _Bot()
        bot.fail = True
        service = TelegramMediaUploadService(bot)  # type: ignore[arg-type]

        with pytest.raises(TelegramMediaUploadError) as captured:
            await service.upload(
                tg_user_id=777,
                kind="photo",
                data=b"photo-bytes",
                filename="cover.jpg",
            )
        assert str(captured.value) == "Telegram media upload failed"
        assert "provider-secret-body" not in str(captured.value)
        assert bot.deleted == []

    asyncio.run(run())
