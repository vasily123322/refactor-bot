from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.userbot.media_download import (
    UserbotMediaDownloadError,
    download_message_media,
)


class _Client:
    def __init__(self, *, size: int, data: bytes = b"image-bytes") -> None:
        self.size = size
        self.data = data
        self.download_calls = 0

    async def get_entity(self, target):
        return SimpleNamespace(id=target)

    async def get_messages(self, entity, *, ids: int):
        return SimpleNamespace(
            id=ids,
            photo=object(),
            video_note=None,
            gif=None,
            voice=None,
            video=None,
            audio=None,
            sticker=None,
            document=None,
            file=SimpleNamespace(
                mime_type="image/jpeg",
                size=self.size,
                width=1200,
                height=630,
                duration=None,
            ),
        )

    async def download_media(self, message, *, file, progress_callback):
        self.download_calls += 1
        progress_callback(len(self.data), len(self.data))
        return self.data


def test_download_message_media_returns_safe_descriptor_and_bytes() -> None:
    async def run() -> None:
        client = _Client(size=11, data=b"hello-media")
        result = await download_message_media(
            SimpleNamespace(_client=client),
            target=-100123,
            message_id=55,
            max_bytes=1024,
        )
        assert result.data == b"hello-media"
        assert result.media.kind == "photo"
        assert result.media.mime_type == "image/jpeg"
        assert result.media.width == 1200
        assert client.download_calls == 1

    asyncio.run(run())


def test_download_message_media_rejects_known_oversize_before_download() -> None:
    async def run() -> None:
        client = _Client(size=2048)
        with pytest.raises(UserbotMediaDownloadError, match="exceeds Studio limit"):
            await download_message_media(
                SimpleNamespace(_client=client),
                target=-100123,
                message_id=55,
                max_bytes=1024,
            )
        assert client.download_calls == 0

    asyncio.run(run())


def test_download_message_media_redacts_provider_exception() -> None:
    class BrokenClient(_Client):
        async def get_messages(self, entity, *, ids: int):
            raise RuntimeError("secret-mtproto-provider-detail")

    async def run() -> None:
        with pytest.raises(UserbotMediaDownloadError) as raised:
            await download_message_media(
                SimpleNamespace(_client=BrokenClient(size=1)),
                target=-100123,
                message_id=55,
            )
        assert str(raised.value) == "Telegram source media download failed"
        assert "secret-mtproto-provider-detail" not in str(raised.value)

    asyncio.run(run())
