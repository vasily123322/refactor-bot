from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from app.core.config import settings
from app.bot.routers.shared import is_admin_user as _is_admin_user
from app.bot.routers.shared import parse_int_from_callback as _parse_int_from_callback
from app.bot.routers.shared import safe_clear_markup as _safe_clear_markup
from app.core.db import AsyncSessionLocal
from app.repositories.admin import AdminConfigRepo, BansRepo
from app.repositories.channels import ChannelsRepo
from app.bot.bot_instance import bot as tg_bot


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def _is_admin(message: Message) -> bool:
    try:
        admin_id = (
            int(settings.admin_user_id) if settings.admin_user_id is not None else None
        )
        uid = int(getattr(message.from_user, "id", 0) or 0)
        return bool(admin_id) and (uid == admin_id)
    except Exception:
        return False


@router.message(Command("remove_allrepeat"))
async def cmd_remove_allrepeat_disabled(message: Message):
    """Block the legacy PostTask-only bulk mutation during canonical migration.

    The historical handler in ``main.py`` rewrites every PostTask repeat/delete field
    without updating canonical ScheduleEntry/Publication authority. Retired canonical
    occurrences may already have no PostTask at all, so that command can neither stop
    the canonical repeat series nor safely cancel canonical destructive intent. Keep an
    earlier admin-router handler as a fail-closed guard until a canonical-aware control
    plane can atomically operate on both authority domains.
    """

    if not _is_admin(message):
        return await message.answer("Недоступно")
    return await message.answer(
        "Команда /remove_allrepeat временно отключена: legacy bulk-изменение PostTask "
        "не является authority для уже переведённых canonical публикаций. "
        "Используйте точечное управление публикациями; автоматическая массовая "
        "мутация repeat/autodelete не выполнялась."
    )


@router.message(Command("panel"))
async def cmd_panel(message: Message):
    if not _is_admin(message):
        return await message.answer("Доступ запрещён")
    # Заголовок и список команд
    text = (
        "админка\n\n"
        "/set_log_channel — назначить канал/чат для логов (перешлите сюда любое сообщение из нужного канала/чата)\n"
        "/bans — список забаненных чатов/каналов\n"
        "/help_admin — описание команд\n"
    )
    await message.answer(text)


@router.message(Command("help_admin"))
async def cmd_help_admin(message: Message):
    if not _is_admin(message):
        return
    text = (
        "Команды админа:\n"
        "/panel — открыть панель\n"
        "/set_log_channel — сделает последний пересланный чат/канал лог-каналом\n"
        "/bans — показать и управление банами\n\n"
        "Кнопки в логах:\n"
        "• Удалить — удалить канал/чат из бота\n"
        "• Забанить — запретить добавление этого канала/чата\n"
        "• Разбанить — снять запрет\n"
    )
    await message.answer(text)


@router.message(Command("set_log_channel"))
async def cmd_set_log_channel(message: Message):
    if not _is_admin(message):
        return
    # Варианты ввода:
    # 1) /set_log_channel <id|@username>
    # 2) Ответ командой на пересланное сообщение из канала/чата
    # 3) Ответ командой на пост канала (sender_chat)
    text = (message.text or "").strip()
    arg_chat = None
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.startswith("@"):
            arg_chat = arg
        else:
            try:
                arg_chat = int(arg)
            except Exception:
                arg_chat = None
    reply = getattr(message, "reply_to_message", None)
    chat_id = None
    if arg_chat is not None:
        # Попробуем resolve через get_chat (для @username либо int id)
        try:
            info = await tg_bot.get_chat(arg_chat)
            chat_id = int(getattr(info, "id", 0) or 0)
        except Exception:
            chat_id = None
    elif reply is not None:
        # 1) sender_chat (если это пост канала)
        sc = getattr(reply, "sender_chat", None)
        if sc:
            chat_id = int(getattr(sc, "id", 0) or 0)
        # 2) forward_from_chat (старые клиенты) или forward_origin.chat (aiogram v3)
        if not chat_id:
            ffc = getattr(reply, "forward_from_chat", None)
            if ffc:
                try:
                    chat_id = int(getattr(ffc, "id", 0) or 0)
                except Exception:
                    chat_id = None
        if not chat_id:
            fwd = getattr(reply, "forward_origin", None)
            ch = getattr(fwd, "chat", None) if fwd else None
            if ch:
                try:
                    chat_id = int(getattr(ch, "id", 0) or 0)
                except Exception:
                    chat_id = None
    # Фолбэк: запрещаем ставить приватный диалог с ботом
    if not chat_id:
        return await message.answer(
            "Не удалось определить канал/чат. Укажите /set_log_channel @username или /set_log_channel -100..., либо ответьте командой на пересланное сообщение из нужного канала/чата."
        )
    # Проверим тип чата (должен быть channel/supergroup/group)
    try:
        info2 = await tg_bot.get_chat(chat_id)
        ctype = str(getattr(info2, "type", ""))
        if ctype == "private":
            return await message.answer(
                "Нельзя назначить личный чат в качестве лог-канала. Укажите канал, супергруппу или группу."
            )
    except Exception:
        pass
    async with AsyncSessionLocal() as session:
        repo = AdminConfigRepo(session)
        await repo.set_log_chat(chat_id)
    await message.answer("Лог-канал обновлён ✅")


@router.message(Command("bans"))
async def cmd_bans(message: Message):
    if not _is_admin(message):
        return
    async with AsyncSessionLocal() as session:
        repo = BansRepo(session)
        items = await repo.list_bans()
    if not items:
        return await message.answer("Банов нет")
    lines = ["Забаненные чаты/каналы:"]
    for b in items:
        lines.append(f"• {b.tg_chat_id} — {b.reason or '-'}")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("admin_ban:"))
async def cb_admin_ban(callback: CallbackQuery):
    # admin_ban:<tg_chat_id>
    uid = int(getattr(callback.from_user, "id", 0) or 0)
    if not _is_admin_user(uid):
        return await callback.answer("Нет доступа")
    chat_id = _parse_int_from_callback(callback, sep=":", idx=1)
    if chat_id is None:
        return await callback.answer("Неверные данные")
    async with AsyncSessionLocal() as session:
        bans = BansRepo(session)
        await bans.ban(chat_id, "admin", uid)
    await callback.answer("Забанен")
    await _safe_clear_markup(callback)


@router.callback_query(F.data.startswith("admin_unban:"))
async def cb_admin_unban(callback: CallbackQuery):
    uid = int(getattr(callback.from_user, "id", 0) or 0)
    if not _is_admin_user(uid):
        return await callback.answer("Нет доступа")
    chat_id = _parse_int_from_callback(callback, sep=":", idx=1)
    if chat_id is None:
        return await callback.answer("Неверные данные")
    async with AsyncSessionLocal() as session:
        bans = BansRepo(session)
        await bans.unban(chat_id)
    await callback.answer("Разбанен")
    await _safe_clear_markup(callback)


@router.callback_query(F.data.startswith("admin_delete:"))
async def cb_admin_delete(callback: CallbackQuery):
    uid = int(getattr(callback.from_user, "id", 0) or 0)
    if not _is_admin_user(uid):
        return await callback.answer("Нет доступа")
    chat_id = _parse_int_from_callback(callback, sep=":", idx=1)
    if chat_id is None:
        return await callback.answer("Неверные данные")
    async with AsyncSessionLocal() as session:
        ch_repo = ChannelsRepo(session)
        ch = await ch_repo.get_by_chat_id(chat_id)
        if not ch:
            return await callback.answer("Не найдено")
        ok = await ch_repo.delete_by_id(ch.id)
    await callback.answer("Удалено" if ok else "Ошибка")
    await _safe_clear_markup(callback)