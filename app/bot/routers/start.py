from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from loguru import logger

from app.bot.keyboards.navigation import home_dashboard_kb, home_dashboard_text
from app.bot.keyboards.reply import main_menu_kb, add_channel_kb
from app.bot.routers.shared import should_show_reply_keyboard


router = Router()
# Ограничим управление ботом только личными чатами
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.message(CommandStart())
async def cmd_start(message: Message):
    show_kb = await should_show_reply_keyboard(message.from_user)
    if show_kb:
        await message.answer(
            home_dashboard_text(inline_mode=False),
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        return

    await message.answer(
        home_dashboard_text(inline_mode=True),
        reply_markup=home_dashboard_kb(),
        parse_mode="HTML",
    )


@router.message(F.text == "Главное меню")
async def rp_main_menu(message: Message):
    await cmd_start(message)


@router.message(F.text == "Добавить канал/чат")
async def rp_add_channel(message: Message):
    text = "➕ Выберите, что подключаем. Telegram запросит необходимые права администратора."
    logger.info(f"UI: open add_channel screen by user_id={message.from_user.id}")
    show_kb = await should_show_reply_keyboard(message.from_user)
    if show_kb:
        await message.answer(text, reply_markup=add_channel_kb())
    else:
        await message.answer(
            text + "\n\nСистемный выбор канала временно откроет нижнюю клавиатуру.",
            reply_markup=add_channel_kb(),
        )


@router.message(F.text == "Добавить канал")
async def rp_add_channel_alias(message: Message):
    await rp_add_channel(message)
