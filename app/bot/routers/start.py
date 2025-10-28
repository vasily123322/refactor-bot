from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from loguru import logger
from app.bot.keyboards.reply import main_menu_kb, add_channel_kb


router = Router()
# Ограничим управление ботом только личными чатами
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.message(CommandStart())
async def cmd_start(message: Message):
	text = "Привет, Я бот для управления каналами."
	await message.answer(text, reply_markup=main_menu_kb())


@router.message(F.text == "Главное меню")
async def rp_main_menu(message: Message):
	await cmd_start(message)


@router.message(F.text == "Добавить канал/чат")
async def rp_add_channel(message: Message):
	text = "Выберите куда подключаем бота"
	logger.info(f"UI: open add_channel screen by user_id={message.from_user.id}")
	await message.answer(text, reply_markup=add_channel_kb())


@router.message(F.text == "Добавить канал")
async def rp_add_channel_alias(message: Message):
	await rp_add_channel(message)




