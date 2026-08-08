from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.builders import build_root_settings_kb
from app.bot.keyboards.navigation import home_dashboard_kb
from app.core.callbacks import CB


router = Router()
router.message.filter(F.chat.type == "private")


def _section_kb(*, label: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=callback_data)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data=CB.GM_GLOBAL_MENU)],
        ]
    )


@router.message(Command("new"))
async def cmd_new_post(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "✍️ <b>Новый пост</b>\n\nВыберите канал и откройте редактор.",
        reply_markup=_section_kb(
            label="✍️ Выбрать канал", callback_data=str(CB.GM_CREATE_POST)
        ),
        parse_mode="HTML",
    )


@router.message(Command("draft"))
async def cmd_new_draft(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "📝 <b>Новый черновик</b>\n\nВыберите канал — публикация останется черновиком до вашего решения.",
        reply_markup=_section_kb(
            label="📝 Выбрать канал", callback_data=str(CB.GM_DRAFT)
        ),
        parse_mode="HTML",
    )


@router.message(Command("plan"))
async def cmd_content_plan(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "📅 <b>Контент-план</b>\n\nОткройте календарь публикаций и очередь по каналам.",
        reply_markup=_section_kb(
            label="📅 Открыть контент-план", callback_data=str(CB.CP_OPEN)
        ),
        parse_mode="HTML",
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "⚙️ <b>Настройки</b>\n\nКаналы, часовой пояс, интерфейс и параметры публикации.",
        reply_markup=build_root_settings_kb(message.from_user.id),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🧭 <b>Быстрые команды</b>\n\n"
        "/new — новый пост\n"
        "/draft — новый черновик\n"
        "/plan — контент-план\n"
        "/settings — настройки\n"
        "/start — главное меню\n\n"
        "Основная работа идёт через inline-кнопки: редактор, AI, расписание и публикация остаются в одном контексте.",
        reply_markup=home_dashboard_kb(),
        parse_mode="HTML",
    )
