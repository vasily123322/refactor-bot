from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from aiogram.fsm.context import FSMContext
from app.bot.fsm.states import SettingsFSM
from aiogram.exceptions import TelegramBadRequest
from contextlib import suppress
from app.core.db import AsyncSessionLocal
from app.repositories.settings import ChannelSettingsRepo
from app.repositories.clients import ClientsRepo
from app.repositories.channels import ChannelsRepo
from app.core.timezone import OFFSET_CITIES
from app.bot.keyboards.builders import build_root_settings_kb as _build_root_settings_kb


router = Router()


@router.callback_query(F.data.startswith("tz_pick:"))
async def cb_tz_pick(callback: CallbackQuery):
    # tz_pick:cid:Europe/Moscow
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, cid_str, tz = parts
    try:
        cid = int(cid_str)
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)
    # Сохраним
    async with AsyncSessionLocal() as session:
        repo = ChannelSettingsRepo(session)
        ok = await repo.update_timezone(cid, tz)
    if not ok:
        return await callback.answer("Не удалось сохранить", show_alert=True)
    # Подтвердим и вернёмся в настройки канала
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(f"✅ Часовой пояс установлен: {tz}")
    # Кнопки назад через отдельное сообщение, чтобы избежать модификации одинакового текста
    with suppress(Exception):
        await callback.message.answer(
            "Вернуться в настройки канала",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Назад", callback_data=f"channels_settings_{cid}"
                        )
                    ]
                ]
            ),
        )
    await callback.answer("Сохранено")


@router.callback_query(F.data.startswith("tz_set_offset:"))
async def cb_tz_set_offset(callback: CallbackQuery):
    # tz_set_offset:cid:minutes
    parts = callback.data.split(":")
    if len(parts) != 3:
        return await callback.answer()
    _, cid_str, mins_str = parts
    try:
        cid = int(cid_str)
        mins = int(mins_str)
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)
    # Сохраним как строку UTC±HH:MM
    sign = "+" if mins >= 0 else "-"
    mins_abs = abs(mins)
    h = mins_abs // 60
    m = mins_abs % 60
    code = f"UTC{sign}{h:02d}:{m:02d}"
    async with AsyncSessionLocal() as session:
        repo = ChannelSettingsRepo(session)
        ok = await repo.update_timezone(cid, code)
    if not ok:
        return await callback.answer("Не удалось сохранить", show_alert=True)
    label_short = f"UTC{sign}{h:02d}"
    # Всплывающее оповещение
    with suppress(Exception):
        await callback.answer(f"Установлено: {label_short}", show_alert=True)
    # Возврат в настройки канала
    with suppress(TelegramBadRequest):
        kb = await _build_root_settings_kb(
            callback.from_user.id if callback.from_user else 0
        )
        await callback.message.edit_text("⚙️ Настройки", reply_markup=kb)


@router.callback_query(F.data.startswith("tz_set_global:"))
async def cb_tz_set_global(callback: CallbackQuery):
    # tz_set_global:minutes
    parts = callback.data.split(":")
    if len(parts) != 2:
        return await callback.answer()
    _, mins_str = parts
    try:
        mins = int(mins_str)
    except Exception:
        return await callback.answer("Ошибка", show_alert=True)
    # Сохраним во всех каналах пользователя
    user = callback.from_user
    if not user:
        return await callback.answer()
    sign = "+" if mins >= 0 else "-"
    mins_abs = abs(mins)
    h = mins_abs // 60
    m = mins_abs % 60
    code = f"UTC{sign}{h:02d}:{m:02d}"
    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        settings_repo = ChannelSettingsRepo(session)
        client = await clients.create_or_get(user.id, user.username, user.full_name)
        items = await channels.list_by_owner(client.id)
        for ch in items:
            await settings_repo.update_timezone(ch.id, code)
    label_short = f"{sign}{h:02d}"
    # Всплывающее оповещение с городами
    cities = OFFSET_CITIES.get(mins, "")
    with suppress(Exception):
        await callback.answer(
            f"Часовой пояс установлен UTC{label_short}{(' - ' + cities) if cities else ''}",
            show_alert=True,
        )
    # Возврат в настройки (корень)
    with suppress(TelegramBadRequest):
        kb = await _build_root_settings_kb(user.id)
        await callback.message.edit_text(
            "Меню настроек бота\nЗдесь можно настроить работу канала или чата и параметры самого бота",
            reply_markup=kb,
        )


@router.message(SettingsFSM.tz_search_input)
async def on_tz_search_input(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = int(data.get("channel_id"))
    query = (message.text or "").strip()
    if not query:
        return await message.answer("❌ Пустой запрос. Введите город или таймзону.")
    ql = query.lower()
    # Алиасы городов
    try:
        from app.bot.routers.main import CITY_ALIASES as _CITY_ALIASES  # type: ignore
    except Exception:
        _CITY_ALIASES = {}
    direct = _CITY_ALIASES.get(ql)
    candidates: list[str] = []
    if direct:
        candidates.append(direct)
    # Поиск по IANA списку
    try:
        from zoneinfo import available_timezones

        all_tz = list(available_timezones())
    except Exception:
        all_tz = []
    for tz in all_tz:
        tl = tz.lower()
        if ql in tl:
            candidates.append(tz)
            if len(candidates) >= 25:
                break
    # Уникализуем
    seen: set[str] = set()
    uniq: list[str] = []
    for tz in candidates:
        if tz not in seen:
            seen.add(tz)
            uniq.append(tz)
    if not uniq:
        return await message.answer("Ничего не найдено. Попробуйте уточнить запрос.")
    rows = [
        [InlineKeyboardButton(text=tz, callback_data=f"tz_pick:{cid}:{tz}")]
        for tz in uniq
    ]
    rows.append(
        [InlineKeyboardButton(text="Отмена", callback_data=f"channels_settings_{cid}")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("Найдены варианты:", reply_markup=kb)
