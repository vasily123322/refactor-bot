from contextlib import suppress
from typing import Mapping, Any
from aiogram.exceptions import TelegramBadRequest
from app.core.timezone import offset_minutes_from_tz as _tz_offset_minutes_from_tz
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo


def escape_markdown_label(label: str) -> str:
    """Escape minimal chars for Telegram Markdown label to avoid breaking links."""
    label = label.replace("[", "\\[")
    label = label.replace("]", "\\]")
    label = label.replace("(", "\\(")
    label = label.replace(")", "\\)")
    label = label.replace("*", "\\*")
    label = label.replace("_", "\\_")
    return label


def offset_minutes_from_tz(code: str | None) -> int:
    """Delegate to core.timezone implementation for unified behavior."""
    return _tz_offset_minutes_from_tz(code)


async def safe_answer(
    callback, text: str | None = None, show_alert: bool = False
) -> None:
    """Safely answer a callback to remove Telegram spinner without raising TelegramBadRequest."""
    with suppress(TelegramBadRequest):
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)


async def safe_edit_reply_markup(
    bot, chat_id: int, message_id: int, reply_markup
) -> None:
    """Safely edit reply markup, ignoring TelegramBadRequest."""
    with suppress(TelegramBadRequest):
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )


async def load_user_ui_context(user) -> tuple[dict[str, bool], int | None, int]:
    async with AsyncSessionLocal() as session:
        repo = ClientsRepo(session)
        client = await repo.create_or_get(user.id, user.username, user.full_name)
        toggles = await repo.get_ui_settings(client.id)
        last_channel_id = await repo.get_last_channel_id(client.id)
    return toggles, last_channel_id, int(client.id)


async def should_show_reply_keyboard(user) -> bool:
    toggles, _, _ = await load_user_ui_context(user)
    return not bool(toggles.get("hide_bottom_menu", False))


def is_ai_enabled(state_data: Mapping[str, Any] | None) -> bool:
    if not state_data:
        return False
    ui_settings = state_data.get("ui_settings") if hasattr(state_data, "get") else None
    if isinstance(ui_settings, dict):
        return bool(ui_settings.get("ai_compose", False))
    return False


async def update_last_channel(user, client_id: int, channel_id: int | None) -> None:
    async with AsyncSessionLocal() as session:
        repo = ClientsRepo(session)
        await repo.create_or_get(user.id, user.username, user.full_name)
        await repo.update_last_channel_id(client_id, channel_id)


async def apply_preview_media(
    bot, chat_id: int, prev_message_id: int | None, payload: dict
) -> None:
    """Update preview media (photo/video) for post editor based on payload fields.

    Expects keys: type (photo|video), file_id, caption/text, media_spoiler, media_pos
    """
    if not prev_message_id:
        return
    try:
        cap = payload.get("caption") or payload.get("text") or ""
        cap = cap[:1024] + ("…" if len(cap) > 1024 else "")
        show_above = str(payload.get("media_pos")) == "bottom"
        media_type = payload.get("type")
        if media_type == "photo":
            from aiogram.types import InputMediaPhoto

            media = InputMediaPhoto(
                media=payload.get("file_id"),
                caption=cap,
                parse_mode="Markdown",
                has_spoiler=bool(payload.get("media_spoiler", False)),
                show_caption_above_media=show_above,
            )
            with suppress(TelegramBadRequest):
                await bot.edit_message_media(
                    chat_id=chat_id, message_id=int(prev_message_id), media=media
                )
        elif media_type == "video":
            from aiogram.types import InputMediaVideo

            media = InputMediaVideo(
                media=payload.get("file_id"),
                caption=cap,
                parse_mode="Markdown",
                has_spoiler=bool(payload.get("media_spoiler", False)),
                show_caption_above_media=show_above,
            )
            with suppress(TelegramBadRequest):
                await bot.edit_message_media(
                    chat_id=chat_id, message_id=int(prev_message_id), media=media
                )
    except Exception:
        # Silent by design to avoid breaking UX in editor flows
        return


async def back_restore_preview_from_caption(
    callback, state, send_preview_fn, tg_bot
) -> bool:
    """Общий сценарий возврата из режима ввода подписи к предпросмотру."""
    try:
        from app.bot.fsm.states import PostFSM

        cur = await state.get_state()
        if cur != PostFSM.caption.state:
            return False
        data = await state.get_data()
        payload = dict(data.get("payload") or {})
        prev_id = data.get("preview_msg_id")
        if prev_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=prev_id
                )
        notify_on = bool(data.get("notify_on", True))
        autosign_on = bool(data.get("autosign_on", False))
        pin_on = bool(data.get("pin_on", False))
        comments_on = bool(data.get("comments_on", True))
        preview2 = await send_preview_fn(
            callback.message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=bool(data.get("is_draft", False)),
            edit_mode=(
                data.get("edit_chat_id") is not None
                and data.get("edit_msg_id") is not None
                and not bool(data.get("is_draft", False))
            ),
            has_buttons=False,
            state=state,
        )
        await state.update_data(preview_msg_id=preview2.message_id)
        await state.set_state(PostFSM.preview)
        with suppress(TelegramBadRequest):
            await callback.answer()
        return True
    except Exception:
        return False


async def back_restore_preview_from_content(
    callback, state, send_preview_fn, tg_bot
) -> bool:
    """Общий сценарий возврата из режима замены медиа к предпросмотру с меню «Медиа» для редактирования."""
    try:
        from app.bot.fsm.states import PostFSM
        from app.core.callbacks import CB
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        cur = await state.get_state()
        if cur != PostFSM.content.state:
            return False
        data_back = await state.get_data()
        payload_back = dict(data_back.get("payload") or {})
        prompt_id = data_back.get("media_prompt_id") or data_back.get("preview_msg_id")
        if prompt_id:
            with suppress(TelegramBadRequest):
                await tg_bot.delete_message(
                    chat_id=callback.message.chat.id, message_id=int(prompt_id)
                )
        notify_on = bool(data_back.get("notify_on", True))
        autosign_on = bool(data_back.get("autosign_on", False))
        pin_on = bool(data_back.get("pin_on", False))
        comments_on = bool(data_back.get("comments_on", True))
        preview2 = await send_preview_fn(
            callback.message,
            payload_back,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=bool(data_back.get("is_draft", False)),
            edit_mode=(
                data_back.get("edit_chat_id") is not None
                and data_back.get("edit_msg_id") is not None
                and not bool(data_back.get("is_draft", False))
            ),
            has_buttons=False,
            state=state,
        )
        # В режиме редактирования можно показать меню Медиа
        is_edit_mode = (
            data_back.get("edit_chat_id") is not None
            and data_back.get("edit_msg_id") is not None
            and not bool(data_back.get("is_draft", False))
        )
        if is_edit_mode and (
            payload_back.get("type")
            in {"photo", "video", "animation", "audio", "voice", "video_note", "album"}
        ):
            await state.update_data(
                preview_msg_id=preview2.message_id,
                media_prompt_id=None,
                ui_submenu="media",
            )
            pl = payload_back
            pos = pl.get("media_pos", "top")
            spoiler = bool(pl.get("media_spoiler", False))
            label_pos = (
                "Расположение: сверху" if pos != "bottom" else "Расположение: снизу"
            )
            label_sp = "✅ Спойлер" if spoiler else "☑️ Спойлер"
            media_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=label_pos, callback_data=CB.MEDIA_POS_TOGGLE
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=label_sp, callback_data=CB.MEDIA_SPOILER_TOGGLE
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Заменить", callback_data=CB.MEDIA_REPLACE
                        )
                    ],
                    [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_BACK)],
                ]
            )
            with suppress(TelegramBadRequest):
                await tg_bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=preview2.message_id,
                    reply_markup=media_kb,
                )
        else:
            await state.update_data(
                preview_msg_id=preview2.message_id,
                media_prompt_id=None,
                ui_submenu=None,
            )
        await state.set_state(PostFSM.preview)
        with suppress(TelegramBadRequest):
            await callback.answer()
        return True
    except Exception:
        return False


async def build_preview_kb(state_data: dict):
    """Build preview keyboard for post editor based on state snapshot.

    Considers payload type, text/caption presence, notify/pin/comments flags,
    draft/edit mode and merges user-provided buttons above the editor actions.
    """
    # Lazy imports to avoid cyclic dependencies at module import time
    from app.bot.keyboards.posting import post_actions
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    payload: dict = dict(state_data.get("payload") or {})
    for_video_note: bool = payload.get("type") == "video_note"
    # Determine text presence depending on content type
    has_text: bool = False
    if payload.get("type") == "text":
        has_text = bool((payload.get("text") or "").strip())
    elif payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "video_note",
        "album",
    }:
        has_text = bool((payload.get("caption") or "").strip())

    edit_mode: bool = (
        state_data.get("edit_chat_id") is not None
        and state_data.get("edit_msg_id") is not None
        and not bool(state_data.get("is_draft", False))
    )

    base_kb = post_actions(
        for_video_note=for_video_note,
        has_media=payload.get("type")
        in {"photo", "video", "animation", "audio", "voice", "video_note", "album"},
        notify_on=bool(state_data.get("notify_on", True)),
        autosign_on=bool(state_data.get("autosign_on", False)),
        pin_on=bool(state_data.get("pin_on", False)),
        comments_on=bool(state_data.get("comments_on", True)),
        is_draft=bool(state_data.get("is_draft", False)),
        edit_mode=edit_mode,
        has_text=has_text,
        has_buttons=bool((payload.get("buttons") or [])),
        ai_enabled=is_ai_enabled(state_data),
    )

    # Merge user-defined buttons above editor controls if present
    if payload.get("buttons") or []:
        user_rows = []
        for r in payload.get("buttons"):
            row_btns = []
            for b in r:
                row_btns.append(
                    InlineKeyboardButton(text=b.get("text", "Button"), url=b.get("url"))
                )
            user_rows.append(row_btns)
        return InlineKeyboardMarkup(
            inline_keyboard=user_rows + (base_kb.inline_keyboard or [])
        )
    return base_kb


def parse_duration_free(text: str) -> int | None:
    """Parse human-friendly duration to seconds.

    Supported examples: "6", "6:30", "6 30", "730", "6ч", "2д", "2д 6ч", "46 мин".
    Max 30 days.
    """
    import re as _re

    try:
        t = (text or "").strip().lower()
        if not t:
            return None
        days = 0
        hours = 0
        minutes = 0
        m_days = _re.findall(r"(\d+)\s*д", t)
        m_hours = _re.findall(r"(\d+)\s*ч", t)
        m_mins = _re.findall(r"(\d+)\s*(?:м|мин)\b", t)
        if m_days:
            try:
                days = int(m_days[0])
            except Exception:
                days = 0
        if m_hours:
            try:
                hours = int(m_hours[0])
            except Exception:
                hours = 0
        if m_mins:
            try:
                minutes = int(m_mins[0])
            except Exception:
                minutes = 0
        if days or hours or minutes:
            total_seconds = days * 24 * 3600 + hours * 3600 + minutes * 60
            if total_seconds > 30 * 24 * 3600:
                total_seconds = 30 * 24 * 3600
            return total_seconds
        m = _re.match(r"^(\d{1,2})[:\s](\d{1,2})$", t)
        if m:
            hours = int(m.group(1))
            minutes = int(m.group(2))
            if minutes >= 60:
                return None
            total_seconds = hours * 3600 + minutes * 60
            if total_seconds > 30 * 24 * 3600:
                total_seconds = 30 * 24 * 3600
            return total_seconds
        if _re.match(r"^\d{1,2}$", t):
            hours = int(t)
            if hours <= 0:
                return None
            total_seconds = hours * 3600
            if total_seconds > 30 * 24 * 3600:
                total_seconds = 30 * 24 * 3600
            return total_seconds
        if _re.match(r"^\d{3,4}$", t):
            if len(t) == 3:
                hours = int(t[0])
                minutes = int(t[1:])
            else:
                hours = int(t[:-2])
                minutes = int(t[-2:])
            if minutes >= 60:
                return None
            total_seconds = hours * 3600 + minutes * 60
            if total_seconds > 30 * 24 * 3600:
                total_seconds = 30 * 24 * 3600
            return total_seconds
        return None
    except Exception:
        return None


async def resolve_original_post_ref(message, bot) -> tuple[int | None, int | None]:
    """Resolve original channel chat_id and message_id from a forwarded post message.

    Tries in order:
    1) message.forward_origin.chat/message_id (new Telegram model)
    2) message.forward_from_chat/forward_from_message_id (legacy model)
    3) t.me link in message.text (supports c/<id>/<msg> and @username/<msg>)
    Returns tuple (orig_chat_id, orig_msg_id) or (None, None) if not resolvable.
    """
    try:
        orig_chat_id: int | None = None
        orig_msg_id: int | None = None

        # 1) New model: forward_origin.channel
        origin = getattr(message, "forward_origin", None)
        if (
            origin
            and getattr(origin, "chat", None)
            and getattr(origin, "message_id", None)
        ):
            try:
                orig_chat_id = int(origin.chat.id)
                orig_msg_id = int(origin.message_id)
            except Exception:
                pass

        # 2) Legacy model: forward_from_chat
        if (
            (orig_chat_id is None or orig_msg_id is None)
            and getattr(message, "forward_from_chat", None)
            and getattr(message, "forward_from_message_id", None)
        ):
            try:
                orig_chat_id = int(message.forward_from_chat.id)
                orig_msg_id = int(message.forward_from_message_id)
            except Exception:
                pass

        # 3) t.me link
        if (orig_chat_id is None or orig_msg_id is None) and (
            message.text or ""
        ).startswith("http"):
            import re as _re

            txt = message.text or ""
            m = _re.search(
                r"t\.me/(?:c/([0-9]+)/([0-9]+)|([A-Za-z0-9_]+)/([0-9]+))", txt
            )
            if m:
                if m.group(1) and m.group(2):
                    try:
                        orig_chat_id = int("-100" + m.group(1))
                        orig_msg_id = int(m.group(2))
                    except Exception:
                        pass
                elif m.group(3) and m.group(4):
                    uname = m.group(3)
                    try:
                        chat = await bot.get_chat(uname)
                        orig_chat_id = int(chat.id)
                        orig_msg_id = int(m.group(4))
                    except Exception:
                        pass

        return orig_chat_id, orig_msg_id
    except Exception:
        return None, None


def build_payload_from_message(
    message, *, prefer_html_caption: bool = False, include_entities: bool = False
) -> dict | None:
    """Build post payload dict from aiogram Message.

    Options:
    - prefer_html_caption: when True, uses message.html_text as caption/text when available
    - include_entities: when True, attaches caption_entities/entities dumps (for main editor flow)
    Returns payload dict or None if content cannot be determined.
    """
    try:
        # Helpers to pick caption/text consistently
        def _pick_caption() -> str:
            if prefer_html_caption and getattr(message, "html_text", None):
                return message.html_text or ""
            return message.caption or ""

        def _pick_text() -> str:
            if prefer_html_caption and getattr(message, "html_text", None):
                return message.html_text or (message.text or "")
            return message.text or ""

        payload: dict | None = None
        if message.photo:
            ph = message.photo[-1]
            payload = {
                "type": "photo",
                "file_id": ph.file_id,
                "caption": _pick_caption(),
            }
            if include_entities and getattr(message, "caption_entities", None):
                payload["caption_entities"] = [
                    e.model_dump() for e in (message.caption_entities or [])
                ]
        elif message.video:
            payload = {
                "type": "video",
                "file_id": message.video.file_id,
                "caption": _pick_caption(),
            }
            if include_entities and getattr(message, "caption_entities", None):
                payload["caption_entities"] = [
                    e.model_dump() for e in (message.caption_entities or [])
                ]
        elif message.animation:
            payload = {
                "type": "animation",
                "file_id": message.animation.file_id,
                "caption": _pick_caption(),
            }
            if include_entities and getattr(message, "caption_entities", None):
                payload["caption_entities"] = [
                    e.model_dump() for e in (message.caption_entities or [])
                ]
        elif message.audio:
            payload = {
                "type": "audio",
                "file_id": message.audio.file_id,
                "caption": _pick_caption(),
            }
            if include_entities and getattr(message, "caption_entities", None):
                payload["caption_entities"] = [
                    e.model_dump() for e in (message.caption_entities or [])
                ]
        elif message.voice:
            payload = {
                "type": "voice",
                "file_id": message.voice.file_id,
                "caption": _pick_caption(),
            }
            if include_entities and getattr(message, "caption_entities", None):
                payload["caption_entities"] = [
                    e.model_dump() for e in (message.caption_entities or [])
                ]
        elif message.video_note:
            payload = {
                "type": "video_note",
                "file_id": message.video_note.file_id,
                "caption": _pick_caption(),
            }
            if include_entities and getattr(message, "caption_entities", None):
                payload["caption_entities"] = [
                    e.model_dump() for e in (message.caption_entities or [])
                ]
        elif message.text or getattr(message, "html_text", None):
            payload = {"type": "text", "text": _pick_text()}
            if include_entities and getattr(message, "entities", None):
                payload["entities"] = [e.model_dump() for e in (message.entities or [])]

        return payload
    except Exception:
        return None


def is_admin_user(user_id: int | None) -> bool:
    """Проверка, является ли пользователь админом по settings.admin_user_id."""
    try:
        from app.core.config import settings as _settings

        return bool(_settings.admin_user_id) and (
            int(user_id or 0) == int(_settings.admin_user_id)
        )
    except Exception:
        return False


def parse_int_from_callback(callback, *, sep: str = ":", idx: int = 1) -> int | None:
    """Безопасно распарсить int из callback.data по разделителю."""
    try:
        parts = str(getattr(callback, "data", "")).split(sep)
        return int(parts[idx]) if len(parts) > idx else None
    except Exception:
        return None


async def safe_clear_markup(callback) -> None:
    """Безопасно снять разметку сообщения, игнорируя ошибки."""
    try:
        await callback.message.edit_reply_markup()
    except Exception:
        return
