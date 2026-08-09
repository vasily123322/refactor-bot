from __future__ import annotations

from dataclasses import dataclass

from app.userbot.client import (
    UserbotGateway,
    UserbotMedia,
    _adapt_media,
    _public_target,
)


MAX_SOURCE_MEDIA_BYTES = 20 * 1024 * 1024


class UserbotMediaDownloadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadedUserbotMedia:
    data: bytes
    media: UserbotMedia


async def download_message_media(
    gateway: UserbotGateway,
    *,
    target: str | int,
    message_id: int,
    max_bytes: int = MAX_SOURCE_MEDIA_BYTES,
) -> DownloadedUserbotMedia:
    """Re-fetch one source message and download its media without persisting MTProto refs."""
    limit = max(1, int(max_bytes))
    try:
        # This helper lives inside app.userbot so the Telethon client remains an
        # implementation detail of the userbot boundary rather than source-domain state.
        client = gateway._client
        entity = await client.get_entity(_public_target(target))
        message = await client.get_messages(entity, ids=int(message_id))
        if message is None:
            raise UserbotMediaDownloadError("Telegram source message not found")
        media = _adapt_media(message)
        if media is None:
            raise UserbotMediaDownloadError("Telegram source message has no media")
        if media.size_bytes is not None and media.size_bytes > limit:
            raise UserbotMediaDownloadError("Telegram source media exceeds Studio limit")

        def progress(downloaded: int, total: int) -> None:
            if int(downloaded or 0) > limit or int(total or 0) > limit:
                raise UserbotMediaDownloadError("Telegram source media exceeds Studio limit")

        data = await client.download_media(message, file=bytes, progress_callback=progress)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise UserbotMediaDownloadError("Telegram source media download returned no data")
        payload = bytes(data)
        if len(payload) > limit:
            raise UserbotMediaDownloadError("Telegram source media exceeds Studio limit")
        return DownloadedUserbotMedia(data=payload, media=media)
    except UserbotMediaDownloadError:
        raise
    except Exception as exc:
        raise UserbotMediaDownloadError("Telegram source media download failed") from exc
