from __future__ import annotations

from contextlib import suppress
from datetime import datetime as _dt, timedelta

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.bot_instance import bot as tg_bot
from app.bot.routers.shared import escape_markdown_label as _escape_markdown_label
from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal
from app.core.timezone import format_user_dt as _format_user_dt
from app.domain.models import Client as _Client
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.settings import ChannelSettingsRepo
from app.services.posting import PostingService


async def maybe_append_autosign(text: str, data: dict) -> str:
    try:
        if not bool(data.get("autosign_on", False)):
            return text or ""
        async with AsyncSessionLocal() as session:
            settings_repo = ChannelSettingsRepo(session)
            st = await settings_repo.get_by_channel_id(
                int(data.get("channel_id", 0) or 0)
            )
        autosign_text = (st.autosign or None) if st else None
        if autosign_text:
            base = text or ""
            return base + ("\n\n" if base else "") + autosign_text
        return text or ""
    except Exception:
        return text or ""


def clip_text_len(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else (text[: max(0, limit - 1)] + "…")


async def edit_text_with_fallback(
    edit_chat_id: int, primary_msg_id: int, candidate_ids: list[int], text: str
) -> int:
    try:
        await tg_bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=primary_msg_id,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return int(primary_msg_id)
    except Exception:
        for cid in reversed(list(candidate_ids or [])):
            with suppress(Exception):
                await tg_bot.edit_message_text(
                    chat_id=edit_chat_id,
                    message_id=int(cid),
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                return int(cid)
    return int(primary_msg_id)


async def edit_media_with_fallback(
    edit_chat_id: int, primary_msg_id: int, candidate_ids: list[int], media
) -> int | None:
    ok = False
    with suppress(Exception):
        await tg_bot.edit_message_media(
            chat_id=edit_chat_id, message_id=primary_msg_id, media=media
        )
        ok = True
        return int(primary_msg_id)
    if not ok:
        for cid in reversed(list(candidate_ids or [])):
            with suppress(Exception):
                await tg_bot.edit_message_media(
                    chat_id=edit_chat_id, message_id=int(cid), media=media
                )
                return int(cid)
    return None


async def apply_album_autosign_entities(payload: dict, data: dict) -> dict:
    # Добавить автоподпись в entities выбранного элемента альбома и обрезать корректно
    items = list(payload.get("items") or [])
    if not items:
        return payload
    idx = len(items) - 1
    for j, it in enumerate(items):
        if (it.get("caption") or "").strip():
            idx = j
    # Получим текст автоподписи, если включено
    autosign_text = ""
    try:
        if bool(data.get("autosign_on", False)):
            async with AsyncSessionLocal() as session:
                settings_repo = ChannelSettingsRepo(session)
                st = await settings_repo.get_by_channel_id(
                    int(data.get("channel_id", 0) or 0)
                )
            autosign_text = (st.autosign or "") if st else ""
    except Exception:
        autosign_text = ""

    # Разберём Markdown-ссылки в автоподписи и превратим их в caption_entities (text_link)
    def _extract_entities(md: str) -> tuple[str, list[dict]]:
        import re as _re

        def _utf16_len(s: str) -> int:
            return len(s.encode("utf-16-le")) // 2

        clean = md
        entities: list[dict] = []
        parts: list[tuple[int, int, dict, str]] = []
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
        parts.sort(key=lambda x: x[0])
        clean_chunks: list[str] = []
        cursor = 0
        for start, end, ent, repl in parts:
            if start < cursor:
                continue
            clean_chunks.append(md[cursor:start])
            offset_in_clean = sum(len(s.encode("utf-16-le")) for s in clean_chunks) // 2
            clean_chunks.append(repl)
            ent2 = dict(ent)
            ent2["offset"] = offset_in_clean
            entities.append(ent2)
            cursor = end
        clean_chunks.append(md[cursor:])
        clean = "".join(clean_chunks)
        return (clean, entities)

    autosign_clean, autosign_entities = _extract_entities(autosign_text)
    cap0 = items[idx].get("caption") or ""
    sep = "\n\n" if cap0 else ""
    combined = cap0 + sep + autosign_clean
    base_entities = list(items[idx].get("caption_entities") or [])

    def _utf16_len2(s: str) -> int:
        return len(s.encode("utf-16-le")) // 2

    shift = _utf16_len2(cap0 + sep)
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
    for e in autosign_entities:
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
    max_len = 1024
    caption_final = (
        combined if len(combined) <= max_len else (combined[: max_len - 1] + "…")
    )
    items[idx]["caption"] = caption_final
    if merged:
        cap_len = _utf16_len2(caption_final)
        pruned: list[dict] = []
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
        if pruned:
            items[idx]["caption_entities"] = pruned
    payload["items"] = items
    return payload


def _iso_to_datetime(defer_iso: str):
    try:
        return _dt.fromisoformat(str(defer_iso))
    except Exception:
        return None


def _apply_repeat_flags_from_state_to_payload(state_data: dict, payload: dict) -> None:
    try:
        if (
            bool(state_data.get("repeat_on", False))
            and int(state_data.get("repeat_seconds") or 0) > 0
        ):
            payload["repeat_on"] = True
            payload["repeat_seconds"] = int(state_data.get("repeat_seconds") or 0)
    except Exception:
        pass


def _set_autodelete_effective(payload: dict) -> None:
    try:
        ad_sec0 = int(payload.get("autodelete_seconds") or 0)
        rep_sec0 = (
            int(payload.get("repeat_seconds") or 0)
            if bool(payload.get("repeat_on", False))
            else 0
        )
        if ad_sec0 > 0 and rep_sec0 > 0 and ad_sec0 <= rep_sec0:
            payload["autodelete_effective_seconds"] = int(max(ad_sec0, rep_sec0 + 5))
    except Exception:
        pass


async def _gate_short_autodelete_in_payload(chan_id: int, payload: dict) -> None:
    try:
        is_pro_timer = await _is_channel_pro(chan_id)
        ad = int(payload.get("autodelete_seconds") or 0)
        if (not is_pro_timer) and ad and ad < 5 * 3600:
            payload.pop("autodelete_seconds", None)
            payload.pop("autodelete_label", None)
            payload.pop("autodelete_effective_seconds", None)
    except Exception:
        pass


async def _apply_autosign_if_enabled(
    chan_id: int, state_data: dict, payload: dict
) -> dict:
    try:
        autosign_on = bool(state_data.get("autosign_on", False))
        if not autosign_on:
            return payload
        async with AsyncSessionLocal() as session:
            repo = ChannelSettingsRepo(session)
            st = await repo.get_by_channel_id(chan_id)
        autosign_text = (st.autosign or None) if st else None
        if autosign_text:
            payload = PostingService.apply_autosign_to_payload(payload, autosign_text)
        payload["autosign_applied"] = True
        return payload
    except Exception:
        return payload


async def _schedule_next_repeat_if_pro(
    service, chan_id: int, payload: dict, state_data: dict, when: _dt
) -> None:
    try:
        is_pro = await _is_channel_pro(chan_id)
        secs_rep = int(state_data.get("repeat_seconds") or 0)
        if not (is_pro and bool(state_data.get("repeat_on", False)) and secs_rep > 0):
            return
        next_when = when + timedelta(seconds=secs_rep)
        pl2 = dict(payload)
        pl2["repeat_on"] = True
        pl2["repeat_seconds"] = secs_rep
        try:
            if payload.get("type") == "text":
                tx = str(pl2.get("text") or "")
                async with AsyncSessionLocal() as session:
                    repo = ChannelSettingsRepo(session)
                    st3 = await repo.get_by_channel_id(chan_id)
                au = ((st3.autosign or "").strip()) if st3 else ""
                if au:
                    mark = "\n\n" + au
                    if tx.endswith(mark):
                        tx = tx[: -len(mark)]
                    elif tx.endswith(au):
                        tx = tx[: -len(au)]
                pl2["text"] = tx
            pl2.pop("autosign_applied", None)
        except Exception:
            pass
        try:
            if payload.get("meta") and not pl2.get("meta"):
                pl2["meta"] = dict(payload["meta"])
        except Exception:
            pass
        await service.schedule(chan_id, pl2, next_when)
    except Exception:
        pass


async def _build_scheduled_confirmation(chan_id: int, defer_iso: str):
    link = None
    title = "канал"
    try:
        async with AsyncSessionLocal() as session:
            ch = await ChannelsRepo(session).get_by_id(chan_id)
            title = ch.title or str(ch.tg_chat_id)
            tg_chat_id = int(ch.tg_chat_id)
        chat = await tg_bot.get_chat(tg_chat_id)
        uname = getattr(chat, "username", None)
        if uname:
            link = f"https://t.me/{uname}"
        else:
            with suppress(Exception):
                inv = await tg_bot.create_chat_invite_link(
                    chat_id=tg_chat_id, name="content-plan", creates_join_request=False
                )
                link = getattr(inv, "invite_link", None)
    except Exception:
        pass
    chan_md = (
        f"[{_escape_markdown_label(title)}]({link})"
        if link
        else _escape_markdown_label(title)
    )
    when = _dt.fromisoformat(defer_iso)
    date_str = _format_user_dt(when, "UTC", "%d.%m.%Y %H:%M UTC")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть контент‑план",
                    callback_data=f"cp_open_cal:{chan_id}:{when.date().isoformat()}",
                )
            ]
        ]
    )
    return f"⏰ Публикация запланирована: {date_str}\nКанал: {chan_md}", kb


async def _render_forward_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    forward_to = set(int(x) for x in (data.get("forward_to") or []))
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        client = await clients.create_or_get(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name,
        )
        items = await channels.list_by_owner(client.id)
    rows: list[list[InlineKeyboardButton]] = []
    for ch in items or []:
        title = (ch.title or str(ch.tg_chat_id))[:40]
        mark = "✅ " if int(ch.id) in forward_to else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{title}",
                    callback_data=f"{CB.POST_FWD_TOGGLE_PREFIX}{ch.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Все", callback_data=CB.POST_FWD_ALL),
            InlineKeyboardButton(text="Ни один", callback_data=CB.POST_FWD_NONE),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_SETTINGS_BACK)]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Выберите каналы для пересылки:", reply_markup=kb
        )
    with suppress(Exception):
        await state.update_data(ui_submenu="forward")


def _add_author_meta_from_user(user, payload: dict) -> dict:
    try:
        uid = int(getattr(user, "id", 0) or 0)
        if uid:
            payload = dict(payload)
            payload.setdefault("meta", {})
            payload["meta"]["author_user_id"] = uid
            payload["meta"]["author_username"] = getattr(user, "username", None)
            payload["meta"]["author_full_name"] = getattr(user, "full_name", None)
        return payload
    except Exception:
        return payload


async def _is_channel_pro(chan_id: int) -> bool:
    try:
        if not chan_id:
            return False
        async with AsyncSessionLocal() as session:
            repo = ChannelsRepo(session)
            ch = await repo.get_by_id(chan_id)
            if ch:
                owner = await session.get(_Client, int(getattr(ch, "owner_id", 0)))
                return bool(getattr(owner, "is_premium", False)) if owner else False
        return False
    except Exception:
        return False
