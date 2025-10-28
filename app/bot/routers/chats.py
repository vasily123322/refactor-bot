from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ChatShared
from contextlib import suppress
from loguru import logger
from aiogram.exceptions import TelegramForbiddenError
from app.bot.bot_instance import bot as tg_bot
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.admin import AdminConfigRepo, BansRepo


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.message(F.text == "Канал")
async def rp_pick_channel_request(message: Message):
	from aiogram.types import KeyboardButtonRequestChat
	from app.bot.keyboards.reply import ChatAdministratorRights
	rights_bot_channel = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=False,
		can_change_info=True,
		can_invite_users=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
		can_post_messages=True,
		can_edit_messages=True,
	)
	rights_user_channel = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=True,
		can_change_info=True,
		can_invite_users=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
		can_post_messages=True,
		can_edit_messages=True,
	)
	kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
		text="Открыть список каналов",
		callback_data="noop",
	) ]])
	from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
	rk = ReplyKeyboardMarkup(
		keyboard=[[KeyboardButton(text="Выбрать канал", request_chat=KeyboardButtonRequestChat(
			request_id=201,
			chat_is_channel=True,
			chat_is_created=False,
			bot_administrator_rights=rights_bot_channel,
			user_administrator_rights=rights_user_channel,
		))]], resize_keyboard=True, one_time_keyboard=True
	)
	await message.answer("Выберите канал", reply_markup=rk)


@router.message(F.text == "Чат")
async def rp_pick_group_request(message: Message):
	from aiogram.types import KeyboardButtonRequestChat
	from app.bot.keyboards.reply import ChatAdministratorRights
	rights_bot_group = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=False,
		can_change_info=True,
		can_invite_users=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
		can_pin_messages=True,
	)
	rights_user_group = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=True,
		can_promote_members=True,
		can_change_info=False,
		can_invite_users=False,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
		can_pin_messages=True,
	)
	from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
	rk = ReplyKeyboardMarkup(
		keyboard=[[KeyboardButton(text="Выбрать чат/группу", request_chat=KeyboardButtonRequestChat(
			request_id=202,
			chat_is_channel=False,
			chat_is_created=False,
			bot_administrator_rights=rights_bot_group,
			user_administrator_rights=rights_user_group,
		))]], resize_keyboard=True, one_time_keyboard=True
	)
	await message.answer("Выберите чат/группу", reply_markup=rk)


@router.message(F.chat_shared)
async def on_chat_shared(message: Message):
	shared: ChatShared = message.chat_shared
	if not shared:
		return
	chat_id = shared.chat_id
	logger.info(f"UI: chat_shared received user_id={message.from_user.id}, chat_id={chat_id}")
	# Блокируем добавление, если чат/канал в бане
	try:
		async with AsyncSessionLocal() as _sess_ban:
			if await BansRepo(_sess_ban).is_banned(chat_id):
				with suppress(Exception):
					await message.answer("❌ Этот канал/чат заблокирован в боте и не может быть добавлен.")
				return
	except Exception:
		pass
	# Проверим права бота в выбранном чате/канале и подсветим недостающие
	try:
		me = await tg_bot.get_me()
		mem = await tg_bot.get_chat_member(chat_id, me.id)
		chat_info = await tg_bot.get_chat(chat_id)
		ctype = getattr(chat_info, "type", "")
		missing = []
		# Для каналов — публикация/редактирование/удаление; для чатов — хотя бы удаление
		if ctype == "channel":
			if not bool(getattr(mem, "can_post_messages", False)):
				missing.append("публикация")
			if not bool(getattr(mem, "can_edit_messages", False)):
				missing.append("редактирование")
			if not bool(getattr(mem, "can_delete_messages", False)):
				missing.append("удаление")
		else:
			if not bool(getattr(mem, "can_delete_messages", False)):
				missing.append("удаление сообщений")
		if missing:
			with suppress(Exception):
				await message.answer(
					"⚠️ Бот добавлен без необходимых прав: " + ", ".join(missing) +
					". Выдайте адм.права боту в выбранном чате/канале."
				)
	except TelegramForbiddenError:
		# Бот не добавлен — пользователь выбрал чат, но не подтвердил добавление бота
		with suppress(Exception):
			await message.answer("⚠️ Бот не добавлен в выбранный чат/канал. Добавьте бота и повторите.")
	except Exception:
		pass
	# Сохраним канал/чат в базе за текущим владельцем (Client.id)
	async with AsyncSessionLocal() as session:
		clients = ClientsRepo(session)
		repo = ChannelsRepo(session)
		client = await clients.create_or_get(message.from_user.id, message.from_user.username, message.from_user.full_name)
		ch = await repo.get_by_chat_id(chat_id)
		if ch is None:
			# попытаемся узнать заголовок
			try:
				chat_info = await tg_bot.get_chat(chat_id)
				title = getattr(chat_info, "title", None) or getattr(chat_info, "full_name", None)
			except Exception:
				title = None
			await repo.create(owner_id=client.id, tg_chat_id=chat_id, title=title)
		else:
			# если владелец не установлен корректно — переназначим
			if ch.owner_id != client.id:
				try:
					chat_info = await tg_bot.get_chat(chat_id)
					title = getattr(chat_info, "title", None) or getattr(chat_info, "full_name", None)
				except Exception:
					title = None
				await repo.assign_owner_and_title(ch.id, client.id, title)

	# Ответ без ссылок: определим тип чата и подтвердим подключение
	try:
		chat = await tg_bot.get_chat(chat_id)
		is_channel = (getattr(chat, "type", "") == "channel")
	except Exception:
		is_channel = False
	text = "✅ Канал подключён." if is_channel else "✅ Чат подключён."
	# Кнопка быстрого перехода к списку каналов
	kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть список каналов/чатов", callback_data="settings_channels_list")]])
	await message.answer(text, reply_markup=kb)

	# Лог администратору: #добавил <user_link>, Канал: <link>
	try:
		async with AsyncSessionLocal() as session:
			admin_cfg = AdminConfigRepo(session)
			log_chat_id = await admin_cfg.get_log_chat_id()
			if log_chat_id:
				# Ссылка на пользователя
				uid = int(getattr(message.from_user, "id", 0) or 0)
				uname = getattr(message.from_user, "username", None)
				fname = getattr(message.from_user, "full_name", None)
				if uname:
					user_link = f"<a href=\"https://t.me/{uname}\">@{uname}</a>"
				else:
					label = (fname or str(uid))
					user_link = f"<a href=\"tg://user?id={uid}\">{label}</a>"
				# Ссылка на канал/чат, как показываем пользователю
				chan_link = None
				chan_title = None
				try:
					info = await tg_bot.get_chat(chat_id)
					chan_title = getattr(info, "title", None) or getattr(info, "full_name", None) or str(chat_id)
					uname2 = getattr(info, "username", None)
					if uname2:
						chan_link = f"https://t.me/{uname2}"
					else:
						with suppress(Exception):
							inv = await tg_bot.create_chat_invite_link(chat_id=chat_id, name="admin-log", creates_join_request=False)
							chan_link = getattr(inv, "invite_link", None)
				except Exception:
					chan_title = str(chat_id)
				text_log = f"#добавил {user_link}\nКанал/чат: "
				if chan_link:
					text_log += f"<a href=\"{chan_link}\">{chan_title}</a>"
				else:
					text_log += chan_title
				kb_admin = InlineKeyboardMarkup(inline_keyboard=[
					[
						InlineKeyboardButton(text="Удалить", callback_data=f"admin_delete:{chat_id}"),
						InlineKeyboardButton(text="Забанить", callback_data=f"admin_ban:{chat_id}"),
					]
				])
				with suppress(Exception):
					await tg_bot.send_message(log_chat_id, text_log, reply_markup=kb_admin, disable_web_page_preview=True)
	except Exception:
		pass



