from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import BufferedInputFile, Message


class TelegramMediaUploadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramMediaUploadResult:
    telegram_file_id: str
    width: int | None = None
    height: int | None = None
    duration_seconds: int | None = None


class TelegramMediaUploadService:
    """Capture a Telegram file_id by staging one uploaded file in the user's own chat."""

    def __init__(self, bot: Bot):
        self.bot = bot

    @staticmethod
    def _photo_result(message: Message) -> TelegramMediaUploadResult:
        if not message.photo:
            raise TelegramMediaUploadError("Telegram did not return a photo")
        photo = max(
            message.photo,
            key=lambda item: (
                int(item.width or 0) * int(item.height or 0),
                int(item.file_size or 0),
            ),
        )
        return TelegramMediaUploadResult(
            telegram_file_id=str(photo.file_id),
            width=int(photo.width),
            height=int(photo.height),
        )

    @staticmethod
    def _typed_result(message: Message, kind: str) -> TelegramMediaUploadResult:
        attribute = "voice" if kind == "voice_note" else kind
        media = getattr(message, attribute, None)
        if media is None:
            raise TelegramMediaUploadError(f"Telegram did not return expected {kind}")
        return TelegramMediaUploadResult(
            telegram_file_id=str(media.file_id),
            width=(int(media.width) if getattr(media, "width", None) is not None else None),
            height=(int(media.height) if getattr(media, "height", None) is not None else None),
            duration_seconds=(
                int(media.duration) if getattr(media, "duration", None) is not None else None
            ),
        )

    async def upload(
        self,
        *,
        tg_user_id: int,
        kind: str,
        data: bytes,
        filename: str,
    ) -> TelegramMediaUploadResult:
        if not data:
            raise TelegramMediaUploadError("uploaded media is empty")
        media = BufferedInputFile(data, filename=filename)
        message: Message | None = None
        try:
            if kind == "photo":
                message = await self.bot.send_photo(chat_id=int(tg_user_id), photo=media)
                return self._photo_result(message)
            if kind == "video":
                message = await self.bot.send_video(chat_id=int(tg_user_id), video=media)
            elif kind == "animation":
                message = await self.bot.send_animation(chat_id=int(tg_user_id), animation=media)
            elif kind == "audio":
                message = await self.bot.send_audio(chat_id=int(tg_user_id), audio=media)
            elif kind == "voice_note":
                message = await self.bot.send_voice(chat_id=int(tg_user_id), voice=media)
            else:
                raise TelegramMediaUploadError("unsupported media upload kind")
            return self._typed_result(message, kind)
        except TelegramMediaUploadError:
            raise
        except Exception as exc:
            raise TelegramMediaUploadError("Telegram media upload failed") from exc
        finally:
            if message is not None:
                with suppress(Exception):
                    await self.bot.delete_message(
                        chat_id=int(tg_user_id),
                        message_id=int(message.message_id),
                    )
