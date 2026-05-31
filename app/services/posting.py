from typing import Optional
from datetime import datetime
from loguru import logger
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAnimation,
    FSInputFile,
)
from aiogram.types import InputPaidMediaPhoto, InputPaidMediaVideo
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from app.domain.models import PostTask
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.repositories.posts import PostsRepo
import os
import asyncio
from app.userbot.client import app as userbot
import re as _re

try:
    from pyrogram.types import (
        InlineKeyboardMarkup as PInlineKeyboardMarkup,
        InlineKeyboardButton as PInlineKeyboardButton,
    )
except Exception:
    PInlineKeyboardMarkup = None
    PInlineKeyboardButton = None


class PostingService:
    def __init__(
        self,
        bot: Bot,
        session_or_factory: AsyncSession | async_sessionmaker[AsyncSession],
    ):
        self.bot = bot
        # Поддержка как готовой сессии, так и фабрики сессий
        self.session: Optional[AsyncSession] = (
            session_or_factory if isinstance(session_or_factory, AsyncSession) else None
        )
        self.session_factory: Optional[async_sessionmaker[AsyncSession]] = (
            session_or_factory
            if not isinstance(session_or_factory, AsyncSession)
            else None
        )

    def _clip_text(self, text: str | None, limit: int) -> str:
        if not text:
            return ""
        if len(text) <= limit:
            return text
        # Обрежем аккуратно и добавим многоточие
        return text[: max(0, limit - 1)] + "…"

    async def _send_with_retry(self, func, *args, **kwargs):
        """Call Telegram API with retry on flood control."""
        attempts = 3
        for i in range(attempts):
            try:
                return await func(*args, **kwargs)
            except TelegramRetryAfter as e:
                try:
                    delay = int(getattr(e, "retry_after", 1) or 1)
                except Exception:
                    delay = 1
                # Немного джиттера, чтобы не попасть в ровно тот же слот
                await asyncio.sleep(delay + 1)
                continue
            # Остальные исключения пробрасываем
            except Exception:
                raise

    async def _ensure_square_video_from_video_note(self, file_id: str) -> str | None:
        """Download video_note by file_id and convert to 640x640 MP4 using ffmpeg.
        Returns path to converted file or None on failure."""
        try:
            base_dir = "/home/refactor_bot/var/video_cache"
            os.makedirs(base_dir, exist_ok=True)
            orig_path = os.path.join(base_dir, f"{file_id}.mp4")
            out_path = os.path.join(base_dir, f"{file_id}_640.mp4")
            # If already converted, reuse
            if os.path.exists(out_path):
                logger.info(f"video_note convert: reuse cached {out_path}")
                return out_path
            # Download original if missing
            if not os.path.exists(orig_path):
                try:
                    logger.info(
                        f"video_note convert: downloading {file_id} -> {orig_path}"
                    )
                    await self.bot.download(file_id, destination=orig_path)
                except Exception as e:
                    logger.exception(f"download video_note failed: {e}")
                    return None
            # Convert to square 640x640 via ffmpeg (pad/scale, yuv420p for compatibility)
            logger.info(f"video_note convert: ffmpeg start {orig_path} -> {out_path}")
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                orig_path,
                "-vf",
                "scale=640:640:force_original_aspect_ratio=decrease,pad=640:640:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-an",  # video notes обычно без звука; выберем без аудиодорожки для простоты
                out_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ret = await proc.wait()
            if ret == 0 and os.path.exists(out_path):
                logger.info(f"video_note convert: ffmpeg ok -> {out_path}")
                return out_path
            logger.error(
                f"video_note convert: ffmpeg returned code {ret} for {file_id}"
            )
            return None
        except Exception as e:
            logger.exception(f"video_note convert: square convert failed: {e}")
            return None

    async def send_now(self, channel_id: int, payload: dict) -> list[int] | None:
        try:
            logger.info(
                f"send_now: target_chat_id={channel_id} type={payload.get('type')} text_len={len(payload.get('text', ''))}"
            )
            # Поддержка режима пересылки: если payload содержит forward_from_chat_id/message_id — выполним пересылку
            fwd_chat = payload.get("forward_from_chat_id")
            fwd_msg = payload.get("forward_from_message_id")
            if fwd_chat and fwd_msg:
                try:
                    m = await self._send_with_retry(
                        self.bot.forward_message,
                        chat_id=channel_id,
                        from_chat_id=int(fwd_chat),
                        message_id=int(fwd_msg),
                    )
                    return [m.message_id]
                except Exception:
                    # если переслать не удалось — фолбэк на обычную отправку
                    pass
            return await self._dispatch(channel_id, payload)
        except Exception as e:
            logger.exception(f"send_now failed: {e}")
            return None

    async def schedule(
        self,
        channel_id: int,
        payload: dict,
        when: datetime | None,
        dedupe_key: str | None = None,
    ) -> PostTask:
        # Если передана фабрика сессий — откроем короткоживущую сессию
        if self.session_factory is not None:
            async with self.session_factory() as session:
                repo = PostsRepo(session)
                if dedupe_key:
                    dup = await repo.get_by_dedupe(dedupe_key)
                    if dup:
                        return dup
                post = PostTask(
                    channel_id=channel_id,
                    payload=payload,
                    dedupe_key=dedupe_key,
                    scheduled_at=when,
                )
                session.add(post)
                await session.commit()
                await session.refresh(post)
                return post
        # Иначе используем переданную долгоживущую сессию (обратная совместимость)
        repo = PostsRepo(self.session)  # type: ignore[arg-type]
        if dedupe_key:
            dup = await repo.get_by_dedupe(dedupe_key)
            if dup:
                return dup
        post = PostTask(
            channel_id=channel_id,
            payload=payload,
            dedupe_key=dedupe_key,
            scheduled_at=when,
        )
        self.session.add(post)  # type: ignore[union-attr]
        await self.session.commit()  # type: ignore[union-attr]
        await self.session.refresh(post)  # type: ignore[union-attr]
        return post

    def _build_reply_markup(self, payload: dict) -> InlineKeyboardMarkup | None:
        buttons = payload.get("buttons")
        if not buttons:
            return None
        rows = []
        for row in buttons:
            row_btns = []
            for btn in row:
                row_btns.append(
                    InlineKeyboardButton(
                        text=btn.get("text", "Button"), url=btn.get("url")
                    )
                )
            rows.append(row_btns)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _build_userbot_reply_markup(self, payload: dict):
        """Build Pyrogram InlineKeyboardMarkup from payload buttons if available."""
        if PInlineKeyboardMarkup is None or PInlineKeyboardButton is None:
            return None
        buttons = payload.get("buttons")
        if not buttons:
            return None
        rows = []
        for row in buttons:
            row_btns = []
            for btn in row:
                row_btns.append(
                    PInlineKeyboardButton(
                        text=btn.get("text", "Button"), url=btn.get("url")
                    )
                )
            rows.append(row_btns)
        return PInlineKeyboardMarkup(rows)

    async def _ensure_local_file(
        self, file_id: str, suggested_ext: str = "mp4"
    ) -> str | None:
        """Download a Telegram file by file_id to local cache and return path."""
        try:
            base_dir = "/home/refactor_bot/var/video_cache"
            os.makedirs(base_dir, exist_ok=True)
            local_path = os.path.join(base_dir, f"{file_id}.{suggested_ext}")
            if not os.path.exists(local_path):
                logger.info(f"download: fetching file_id={file_id} -> {local_path}")
                await self.bot.download(file_id, destination=local_path)
            return local_path
        except Exception as e:
            logger.exception(f"download failed for file_id={file_id}: {e}")
            return None

    async def _dispatch(self, channel_id: int, payload: dict) -> list[int]:
        type_ = payload.get("type")
        logger.info(
            f"dispatch: start type={type_} has_caption={bool(payload.get('caption'))} has_buttons={bool(payload.get('buttons'))}"
        )
        reply_markup = self._build_reply_markup(payload)
        silent = bool(payload.get("silent", False))
        common = {"disable_notification": silent}
        ids: list[int] = []
        # Позиционирование медиа относительно текста (для одиночных медиа)
        media_pos: str = str(payload.get("media_pos", "top"))  # top|bottom
        spoiler: bool = bool(payload.get("media_spoiler", False))
        paid_on: bool = bool(payload.get("media_paid_on", False))
        price_stars: int = int(payload.get("media_paid_price") or 0)
        # Поддержка внутреннего типа 'album' — конвертируем в media_group
        if type_ == "album":
            items = list(payload.get("items") or [])
            media: list[dict] = []
            for it in items:
                it_type = it.get("type")
                if it_type not in {"photo", "video", "animation"}:
                    # аудио и прочее в медиагруппе пропускаем (или отправим отдельно в будущем)
                    continue
                media.append(
                    {
                        "type": it_type,
                        "file_id": it.get("file_id"),
                        "file_path": it.get("file_path"),
                        "caption": it.get("caption"),
                        "caption_entities": it.get("caption_entities"),
                    }
                )
            pl2 = dict(payload)
            pl2["type"] = "media_group"
            pl2["media"] = media
            return await self._dispatch(channel_id, pl2)
        if type_ == "text":
            entities = payload.get("entities")
            text_raw = payload.get("text", "")
            text = text_raw if entities else self._clip_text(text_raw, 4096)
            try:
                # По умолчанию превью ссылок отключено; включаем только если явно установлено в payload
                disable_preview = True
                show_above = True
                try:
                    if bool(payload.get("preview_show_on", False)):
                        disable_preview = False
                    show_above = bool(payload.get("preview_show_above", True))
                except Exception:
                    pass
                if not disable_preview:
                    # Спрячем ссылку, подставив невидимую HTML-ссылку, если есть preview_url/URL в тексте
                    url = str((payload.get("preview_url") or "")).strip()
                    if not url:
                        import re as _re

                        m = _re.search(r"https?://\S+", text)
                        url = m.group(0) if m else ""
                        if url:
                            text = text.replace(url, "").strip()
                    invisible = "\u2061"
                    if url:
                        text_html = f"<a href='{url}'>" + invisible + "</a>" + text
                        from aiogram.types import LinkPreviewOptions

                        LinkPreviewOptions(
                            is_disabled=False, show_above_text=show_above
                        )
                        m = await self._send_with_retry(
                            self.bot.send_message,
                            chat_id=channel_id,
                            text=text_html,
                            disable_web_page_preview=False,
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.HTML,
                            **common,
                        )
                    else:
                        m = await self._send_with_retry(
                            self.bot.send_message,
                            chat_id=channel_id,
                            text=text,
                            disable_web_page_preview=True,
                            reply_markup=reply_markup,
                            parse_mode=None if entities else ParseMode.MARKDOWN,
                            entities=entities,
                            **common,
                        )
                else:
                    m = await self._send_with_retry(
                        self.bot.send_message,
                        chat_id=channel_id,
                        text=text,
                        disable_web_page_preview=True,
                        reply_markup=reply_markup,
                        parse_mode=None if entities else ParseMode.MARKDOWN,
                        entities=entities,
                        **common,
                    )
            except TelegramBadRequest as e:
                if "can't parse entities" in str(e).lower():
                    m = await self._send_with_retry(
                        self.bot.send_message,
                        chat_id=channel_id,
                        text=text,
                        disable_web_page_preview=disable_preview,
                        reply_markup=reply_markup,
                        parse_mode=None,
                        **common,
                    )
                else:
                    raise
            ids.append(m.message_id)
        elif type_ == "photo":
            photo = payload.get("file_id") or FSInputFile(payload.get("file_path"))
            text = payload.get("caption") or payload.get("text") or ""
            caption_entities = payload.get("caption_entities")
            cap = text if caption_entities else self._clip_text(text, 1024)
            show_above = media_pos == "bottom"
            if paid_on:
                if price_stars <= 0:
                    raise TelegramBadRequest(
                        "Paid media requires a positive star price"
                    )
                m = await self._send_with_retry(
                    self.bot.send_paid_media,
                    chat_id=channel_id,
                    star_count=price_stars,
                    media=[InputPaidMediaPhoto(media=photo)],
                    caption=cap,
                    parse_mode=None if caption_entities else ParseMode.MARKDOWN,
                    caption_entities=caption_entities,
                    show_caption_above_media=show_above,
                    reply_markup=reply_markup,
                    **common,
                )
                ids.append(m.message_id)
            else:
                try:
                    m = await self._send_with_retry(
                        self.bot.send_photo,
                        chat_id=channel_id,
                        photo=photo,
                        caption=cap,
                        has_spoiler=spoiler,
                        show_caption_above_media=show_above,
                        reply_markup=reply_markup,
                        parse_mode=None if caption_entities else ParseMode.MARKDOWN,
                        caption_entities=caption_entities,
                        **common,
                    )
                except TelegramBadRequest as e:
                    if "can't parse entities" in str(e).lower():
                        m = await self._send_with_retry(
                            self.bot.send_photo,
                            chat_id=channel_id,
                            photo=photo,
                            caption=cap,
                            has_spoiler=spoiler,
                            show_caption_above_media=show_above,
                            reply_markup=reply_markup,
                            parse_mode=None,
                            **common,
                        )
                    else:
                        raise
                ids.append(m.message_id)
        elif type_ == "video":
            video = payload.get("file_id") or FSInputFile(payload.get("file_path"))
            text = payload.get("caption") or payload.get("text") or ""
            caption_entities = payload.get("caption_entities")
            cap = text if caption_entities else self._clip_text(text, 1024)
            show_above = media_pos == "bottom"
            if paid_on:
                if price_stars <= 0:
                    raise TelegramBadRequest(
                        "Paid media requires a positive star price"
                    )
                m = await self._send_with_retry(
                    self.bot.send_paid_media,
                    chat_id=channel_id,
                    star_count=price_stars,
                    media=[InputPaidMediaVideo(media=video)],
                    caption=cap,
                    parse_mode=None if caption_entities else ParseMode.MARKDOWN,
                    caption_entities=caption_entities,
                    show_caption_above_media=show_above,
                    reply_markup=reply_markup,
                    **common,
                )
                ids.append(m.message_id)
            else:
                try:
                    m = await self._send_with_retry(
                        self.bot.send_video,
                        chat_id=channel_id,
                        video=video,
                        caption=cap,
                        has_spoiler=spoiler,
                        show_caption_above_media=show_above,
                        reply_markup=reply_markup,
                        parse_mode=None if caption_entities else ParseMode.MARKDOWN,
                        caption_entities=caption_entities,
                        **common,
                    )
                except TelegramBadRequest as e:
                    if "can't parse entities" in str(e).lower():
                        m = await self._send_with_retry(
                            self.bot.send_video,
                            chat_id=channel_id,
                            video=video,
                            caption=cap,
                            has_spoiler=spoiler,
                            show_caption_above_media=show_above,
                            reply_markup=reply_markup,
                            parse_mode=None,
                            **common,
                        )
                    else:
                        raise
                ids.append(m.message_id)
        elif type_ == "video_note":
            # Two modes: pair mode (video_note + text message) OR convert to video
            file_id = payload.get("file_id")
            pair_mode = payload.get("vn_pair", True)
            use_userbot = payload.get(
                "use_userbot", True
            )  # по умолчанию пробуем через userbot
            if pair_mode and use_userbot and userbot is not None:
                try:
                    logger.info(
                        "dispatch: video_note via userbot pair -> download and send"
                    )
                    # Download video_note locally and send through userbot
                    local_path = (
                        await self._ensure_local_file(file_id, suggested_ext="mp4")
                        if file_id
                        else None
                    )
                    if local_path is None:
                        raise RuntimeError(
                            "userbot pair: failed to prepare local video_note file"
                        )
                    m1 = await userbot.send_video_note(
                        chat_id=channel_id,
                        video_note=local_path,
                        disable_notification=silent,
                    )
                    text = payload.get("caption") or ""
                    if text or payload.get("buttons"):
                        p_markup = self._build_userbot_reply_markup(payload)
                        await userbot.send_message(
                            chat_id=channel_id,
                            text=text,
                            reply_markup=p_markup,
                            disable_web_page_preview=True,
                            parse_mode="markdown",
                            disable_notification=silent,
                        )
                    return [m1.id] if hasattr(m1, "id") else []
                except Exception as e:
                    logger.warning(
                        f"dispatch: userbot pair failed, fallback to bot: {e}"
                    )
            # Fallbacks: aiogram pair mode or conversion
            if pair_mode:
                logger.info(
                    "dispatch: video_note pair mode -> send video_note + message via bot"
                )
                m1 = await self._send_with_retry(
                    self.bot.send_video_note,
                    chat_id=channel_id,
                    video_note=file_id,
                    **common,
                )
                if payload.get("caption") or reply_markup:
                    await self._send_with_retry(
                        self.bot.send_message,
                        chat_id=channel_id,
                        text=payload.get("caption", ""),
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                        **common,
                    )
                ids.append(m1.message_id)
            else:
                # Convert video_note to square MP4 and send as regular video with caption/buttons
                logger.info(
                    f"dispatch: video_note -> try convert to square video, file_id={file_id}"
                )
                video_path = (
                    await self._ensure_square_video_from_video_note(file_id)
                    if file_id
                    else None
                )
                if video_path:
                    logger.info(
                        f"dispatch: sending converted video {video_path} with caption/buttons"
                    )
                    m = await self._send_with_retry(
                        self.bot.send_video,
                        chat_id=channel_id,
                        video=FSInputFile(video_path),
                        caption=payload.get("caption"),
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN,
                        **common,
                    )
                    ids.append(m.message_id)
                else:
                    # Fallback: try to download original and send as video; if still failing, send as video_note
                    try:
                        base_dir = "/home/refactor_bot/var/video_cache"
                        os.makedirs(base_dir, exist_ok=True)
                        orig_path = os.path.join(base_dir, f"{file_id}_orig.mp4")
                        if not os.path.exists(orig_path):
                            logger.info(
                                f"dispatch: fallback download original video_note to {orig_path}"
                            )
                            await self.bot.download(file_id, destination=orig_path)
                        logger.info(
                            f"dispatch: fallback send as regular video from {orig_path}"
                        )
                        m = await self._send_with_retry(
                            self.bot.send_video,
                            chat_id=channel_id,
                            video=FSInputFile(orig_path),
                            caption=payload.get("caption"),
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.MARKDOWN,
                            **common,
                        )
                        ids.append(m.message_id)
                    except Exception as e:
                        logger.exception(
                            f"dispatch: fallback to send_video_note due to error: {e}"
                        )
                        await self.bot.send_video_note(
                            chat_id=channel_id, video_note=file_id
                        )
        elif type_ == "animation":
            animation = payload.get("file_id") or FSInputFile(payload.get("file_path"))
            m = await self.bot.send_animation(
                chat_id=channel_id,
                animation=animation,
                caption=payload.get("caption"),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
                **common,
            )
            ids.append(m.message_id)
        elif type_ == "media_group":
            media_items = []
            for item in payload.get("media", []):
                caption = item.get("caption")
                if item.get("type") == "photo":
                    photo = item.get("file_id") or FSInputFile(item.get("file_path"))
                    # Сохраняем caption_entities, если они есть (не ломаем форматирование);
                    # иначе используем Markdown, чтобы автоподпись с ссылками была кликабельной
                    media_items.append(
                        InputMediaPhoto(
                            media=photo,
                            caption=caption,
                            parse_mode=None
                            if item.get("caption_entities")
                            else ParseMode.MARKDOWN,
                            caption_entities=item.get("caption_entities"),
                        )
                    )
                elif item.get("type") == "video":
                    video = item.get("file_id") or FSInputFile(item.get("file_path"))
                    media_items.append(
                        InputMediaVideo(
                            media=video,
                            caption=caption,
                            parse_mode=None
                            if item.get("caption_entities")
                            else ParseMode.MARKDOWN,
                            caption_entities=item.get("caption_entities"),
                        )
                    )
                elif item.get("type") == "animation":
                    animation = item.get("file_id") or FSInputFile(
                        item.get("file_path")
                    )
                    media_items.append(
                        InputMediaAnimation(
                            media=animation,
                            caption=caption,
                            parse_mode=None
                            if item.get("caption_entities")
                            else ParseMode.MARKDOWN,
                            caption_entities=item.get("caption_entities"),
                        )
                    )
                elif item.get("type") == "audio":
                    # аудио в медиагруппе редко используется; отправим отдельно
                    pass
            if not media_items:
                raise ValueError("Empty media_group")
            if paid_on:
                if price_stars <= 0:
                    raise TelegramBadRequest(
                        "Paid media requires a positive star price"
                    )
                paid_media = []
                for it in payload.get("media", []):
                    if it.get("type") == "photo":
                        pm = it.get("file_id") or FSInputFile(it.get("file_path"))
                        paid_media.append(InputPaidMediaPhoto(media=pm))
                    elif it.get("type") == "video":
                        vm = it.get("file_id") or FSInputFile(it.get("file_path"))
                        paid_media.append(InputPaidMediaVideo(media=vm))
                if not paid_media:
                    raise TelegramBadRequest(
                        "Paid media group supports only photo/video items"
                    )
                # Подпись: последняя непустая в группе
                cap_text = ""
                cap_entities = None
                idx = len(payload.get("media", [])) - 1
                for j, it in enumerate(payload.get("media", [])):
                    if (it.get("caption") or "").strip():
                        idx = j
                if payload.get("media"):
                    cap_text = payload.get("media", [])[idx].get("caption") or ""
                    cap_entities = (
                        payload.get("media", [])[idx].get("caption_entities") or None
                    )
                m = await self._send_with_retry(
                    self.bot.send_paid_media,
                    chat_id=channel_id,
                    star_count=price_stars,
                    media=paid_media,
                    caption=cap_text,
                    parse_mode=None if cap_entities else ParseMode.MARKDOWN,
                    caption_entities=cap_entities,
                    show_caption_above_media=(media_pos == "bottom"),
                    reply_markup=reply_markup,
                    **common,
                )
                ids.append(m.message_id)
            else:
                try:
                    msgs = await self._send_with_retry(
                        self.bot.send_media_group,
                        chat_id=channel_id,
                        media=media_items,
                        **common,
                    )
                except TelegramBadRequest as e:
                    if "can't parse entities" in str(e).lower():
                        for it in media_items:
                            if isinstance(
                                it,
                                (InputMediaPhoto, InputMediaVideo, InputMediaAnimation),
                            ):
                                it.parse_mode = None
                                it.caption_entities = None
                        msgs = await self.bot.send_media_group(
                            chat_id=channel_id, media=media_items, **common
                        )
                    else:
                        raise
                ids.extend([mm.message_id for mm in msgs])
                if reply_markup or payload.get("text"):
                    text2 = payload.get("text", "")
                    try:
                        m2 = await self._send_with_retry(
                            self.bot.send_message,
                            chat_id=channel_id,
                            text=text2,
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.MARKDOWN,
                            **common,
                        )
                    except TelegramBadRequest as e:
                        if "can't parse entities" in str(e).lower():
                            m2 = await self._send_with_retry(
                                self.bot.send_message,
                                chat_id=channel_id,
                                text=text2,
                                reply_markup=reply_markup,
                                parse_mode=None,
                                **common,
                            )
                        else:
                            raise
                    ids.append(m2.message_id)
        elif payload.get("type") == "audio":
            m = await self._send_with_retry(
                self.bot.send_audio,
                chat_id=channel_id,
                audio=payload["file_id"],
                caption=payload.get("caption"),
                parse_mode="Markdown",
                **common,
            )
            ids.append(m.message_id)
        elif payload.get("type") == "voice":
            m = await self._send_with_retry(
                self.bot.send_voice,
                chat_id=channel_id,
                voice=payload["file_id"],
                caption=payload.get("caption"),
                parse_mode=ParseMode.MARKDOWN,
                **common,
            )
            ids.append(m.message_id)
        else:
            raise ValueError(f"Unsupported payload type: {type_}")
        # Если задан таймер автоудаления — запланируем удаление отправленных сообщений
        # Удаление по времени выполняет планировщик/воркер. Здесь не удаляем, чтобы не гоняться с воркером
        try:
            _ = int(payload.get("autodelete_views") or 0)
        except Exception:
            pass

        # Убрано: in-memory автоудаление. Дата удаления вычисляется и хранится в payload (autodelete_at)
        return ids

    @staticmethod
    def apply_autosign_to_payload(payload: dict, autosign_text: str) -> dict:
        """Merge autosign markdown text into payload text/caption/entities.

        Supports types: text, single media (photo/video/animation/audio/voice/video_note), album(media_group).
        Preserves existing entities and merges link-like entities from autosign.
        Applies Telegram limits (4096 for text, 1024 for captions) with safe trimming and entity pruning.
        Returns updated payload (same dict reference).
        """
        if not (autosign_text or "").strip():
            return payload

        def _utf16_len(s: str) -> int:
            return len(s.encode("utf-16-le")) // 2

        def _clip(text: str, limit: int) -> str:
            if len(text) <= limit:
                return text
            return text[: max(0, limit - 1)] + "…"

        def _extract_entities(md: str) -> tuple[str, list[dict]]:
            # Parse minimal markdown to entities: [text](url), raw URLs, @mentions
            parts: list[tuple[int, int, dict, str]] = []
            # [text](url)
            for m in _re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", md):
                text = m.group(1)
                url = m.group(2)
                start, end = m.span()
                parts.append(
                    (
                        start,
                        end,
                        {
                            "type": "text_link",
                            "offset": 0,
                            "length": _utf16_len(text),
                            "url": url,
                        },
                        text,
                    )
                )
            # raw URLs
            for m in _re.finditer(r"https?://[^\s)]+", md):
                start, end = m.span()
                url_text = m.group(0)
                parts.append(
                    (
                        start,
                        end,
                        {"type": "url", "offset": 0, "length": _utf16_len(url_text)},
                        url_text,
                    )
                )
            # @mention
            for m in _re.finditer(r"@[A-Za-z0-9_]{5,}", md):
                start, end = m.span()
                uname = m.group(0)
                parts.append(
                    (
                        start,
                        end,
                        {"type": "mention", "offset": 0, "length": _utf16_len(uname)},
                        uname,
                    )
                )
            # Build clean text and compute entity offsets within it
            parts.sort(key=lambda x: x[0])
            clean_chunks: list[str] = []
            cursor = 0
            out_entities: list[dict] = []
            for start, end, ent, repl in parts:
                if start < cursor:
                    continue
                clean_chunks.append(md[cursor:start])
                offset_in_clean = _utf16_len("".join(clean_chunks))
                clean_chunks.append(repl)
                ent2 = dict(ent)
                ent2["offset"] = offset_in_clean
                out_entities.append(ent2)
                cursor = end
            clean_chunks.append(md[cursor:])
            return ("".join(clean_chunks), out_entities)

        def _merge_text(
            base_text: str, base_entities: list[dict], autosign_md: str, limit: int
        ) -> tuple[str, list[dict], bool]:
            sep = "\n\n" if base_text else ""
            clean, ents_auto = _extract_entities(autosign_md)
            combined = base_text + sep + clean
            shift = _utf16_len(base_text + sep)
            merged: list[dict] = []
            for e in base_entities:
                try:
                    merged.append(
                        {
                            "type": e.get("type"),
                            "offset": int(e.get("offset", 0)),
                            "length": int(e.get("length", 0)),
                            "url": e.get("url"),
                        }
                    )
                except Exception:
                    pass
            for e in ents_auto:
                try:
                    merged.append(
                        {
                            "type": e.get("type"),
                            "offset": shift + int(e.get("offset", 0)),
                            "length": int(e.get("length", 0)),
                            "url": e.get("url"),
                        }
                    )
                except Exception:
                    pass
            final_text = _clip(combined, limit)
            pruned: list[dict] = []
            if merged:
                cap_len = _utf16_len(final_text)
                for e in merged:
                    off = int(e.get("offset", 0))
                    ln = int(e.get("length", 0))
                    if off >= cap_len:
                        continue
                    ln2 = max(0, min(ln, cap_len - off))
                    if ln2 <= 0:
                        continue
                    ent2 = {k: v for k, v in e.items()}
                    ent2["length"] = ln2
                    pruned.append(ent2)
            return final_text, pruned, bool(base_entities) or bool(ents_auto)

        t = payload.get("type")
        if t == "text":
            base_text = payload.get("text", "")
            base_entities = list(payload.get("entities") or [])
            final_text, pruned, had_merged = _merge_text(
                base_text, base_entities, autosign_text, 4096
            )
            payload["text"] = final_text
            if had_merged:
                payload["entities"] = pruned
            return payload

        if t in {"photo", "video", "animation", "audio", "voice", "video_note"}:
            cap0 = payload.get("caption", "")
            base_entities = list(payload.get("caption_entities") or [])
            final_cap, pruned, had_merged = _merge_text(
                cap0, base_entities, autosign_text, 1024
            )
            payload["caption"] = final_cap
            if had_merged:
                payload["caption_entities"] = pruned
            return payload

        if t == "album":
            items = list(payload.get("items") or [])
            if not items:
                return payload
            idx = len(items) - 1
            for j, it in enumerate(items):
                if (it.get("caption") or "").strip():
                    idx = j
            cap0 = items[idx].get("caption") or ""
            base_entities = list(items[idx].get("caption_entities") or [])
            caption_final, pruned, had_merged = _merge_text(
                cap0, base_entities, autosign_text, 1024
            )
            items[idx]["caption"] = caption_final
            if had_merged:
                items[idx]["caption_entities"] = pruned
            payload["items"] = items
            return payload

        if t == "media_group":
            media = list(payload.get("media") or [])
            if not media:
                return payload
            idx = len(media) - 1
            for j, it in enumerate(media):
                if (it.get("caption") or "").strip():
                    idx = j
            cap0 = media[idx].get("caption") or ""
            base_entities = list(media[idx].get("caption_entities") or [])
            caption_final, pruned, had_merged = _merge_text(
                cap0, base_entities, autosign_text, 1024
            )
            media[idx]["caption"] = caption_final
            if had_merged:
                media[idx]["caption_entities"] = pruned
            payload["media"] = media
            return payload

        return payload
