 
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from contextlib import suppress
import html
from datetime import datetime, timezone, timedelta
from app.core.db import AsyncSessionLocal
from app.repositories.clients import ClientsRepo
from app.repositories.channels import ChannelsRepo
from app.core.timezone import OFFSET_CITIES, offset_minutes_from_tz as _offset_minutes_from_tz
from app.core.callbacks import CB
from app.bot.bot_instance import bot as tg_bot
from app.bot.routers.shared import escape_markdown_label as _escape_markdown_label
from app.repositories.external_bots import ExternalBotsRepo, ChannelBotsRepo
from app.repositories.subscribers import SubscribersRepo
from app.repositories.join_requests import JoinRequestsRepo
from app.bot.keyboards.builders import build_menu
from aiogram.fsm.context import FSMContext
from app.bot.fsm.states import BotManageFSM
from types import SimpleNamespace
from app.core.config import settings


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.callback_query(F.data == "bot_manage_req_filters")
async def cb_bot_manage_req_filters(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		flt = await cb_repo.get_request_filters(cid)
	lines = [
		"Фильтры заявок:",
		f"WL usernames: {', '.join(flt.get('whitelist_usernames', []))}",
		f"BL usernames: {', '.join(flt.get('blacklist_usernames', []))}",
		f"WL ids: {', '.join([str(x) for x in flt.get('whitelist_ids', [])])}",
		f"BL ids: {', '.join([str(x) for x in flt.get('blacklist_ids', [])])}",
		f"Stop words: {', '.join(flt.get('stop_words', []))}",
	]
	kb = build_menu([
        ("WL usernames", "req_flt_edit:whitelist_usernames"),
        ("BL usernames", "req_flt_edit:blacklist_usernames"),
        ("WL ids", "req_flt_edit:whitelist_ids"),
        ("BL ids", "req_flt_edit:blacklist_ids"),
        ("Stop words", "req_flt_edit:stop_words"),
    ], back_to=f"bot_manage_{cid}")
	with suppress(Exception):
		await callback.message.edit_text("\n".join(lines), reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data.startswith("req_flt_edit:"))
async def cb_req_flt_edit(callback: CallbackQuery, state: FSMContext):
	key = callback.data.split(":", 1)[-1]
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key=f"reqflt:{key}")
	with suppress(Exception):
		await callback.message.edit_text("Введите значения через запятую (или '-' чтобы очистить):")
	await callback.answer()


async def _build_root_settings_kb(user_id: int) -> InlineKeyboardMarkup:
	# Простая версия корневого меню настроек (без вычисления локального времени)
	rows = [
		[InlineKeyboardButton(text="Каналы/Чаты", callback_data="settings_channels_list")],
		[InlineKeyboardButton(text="Часовой пояс", callback_data="settings_tz_root")],
		[InlineKeyboardButton(text="Главное меню", callback_data=CB.GM_GLOBAL_MENU)],
	]
	return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "Настройки")
async def rp_settings(message: Message):
	kb = await _build_root_settings_kb(message.from_user.id)
	await message.answer("Меню настроек бота\nЗдесь можно настроить работу канала или чата и параметры самого бота", reply_markup=kb)


@router.callback_query(F.data == "settings_tz_root")
async def cb_settings_tz_root(callback: CallbackQuery):
	user = callback.from_user
	if not user:
		return await callback.answer()
	async with AsyncSessionLocal() as session:
		clients = ClientsRepo(session)
		channels = ChannelsRepo(session)
		client = await clients.create_or_get(user.id, user.username, user.full_name)
		items = await channels.list_by_owner(client.id)
	from app.repositories.settings import ChannelSettingsRepo
	current_min = 180
	if items:
		async with AsyncSessionLocal() as session:
			repo = ChannelSettingsRepo(session)
			for ch in items:
				st = await repo.get_by_channel_id(ch.id)
				code = (st.filters or {}).get("tz") if (st and st.filters) else None
				if code:
					current_min = _offset_minutes_from_tz(code)
					break
	def _fmt_offset(mins: int) -> str:
		sign = "+" if mins >= 0 else "-"
		mins = abs(mins)
		h = mins // 60
		return f"UTC{sign}{h:02d}"
	current_label = _fmt_offset(current_min)
	rows: list[list[InlineKeyboardButton]] = []
	cols = 4
	items_btn: list[tuple[str, int]] = [(_fmt_offset(h*60), h*60) for h in range(-12, 15)]
	row: list[InlineKeyboardButton] = []
	for label, val in items_btn:
		mark = "✅ " if label == current_label else ""
		cities = OFFSET_CITIES.get(val, "")
		simple = label.replace("UTC", "")
		title = f"{mark}{simple}{' — ' + cities if cities else ''}"
		row.append(InlineKeyboardButton(text=title, callback_data=f"tz_set_global:{val}"))
		if len(row) == cols:
			rows.append(row)
			row = []
	if row:
		rows.append(row)
	rows.append([InlineKeyboardButton(text="Назад", callback_data="settings_back_root")])
	kb = InlineKeyboardMarkup(inline_keyboard=rows)
	now_local = datetime.now(timezone.utc) + timedelta(minutes=current_min)
	cities = OFFSET_CITIES.get(current_min, "")
	extra = f" — {cities}" if cities else ""
	text = (
		"🌍 Выберите ваш часовой пояс\n\n"
		"Все посты будут публиковаться и отображаться в соответствии с ним\n\n"
		f"Текущий: <b>{current_label}</b>{extra} (<b>{now_local.strftime('%H:%M')}</b>)."
	)
	with suppress(Exception):
		await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
	await callback.answer()


@router.callback_query(F.data == "settings_channels_list")
async def cb_settings_channels_list(callback: CallbackQuery):
	user = callback.from_user
	if not user:
		return await callback.answer()
	async with AsyncSessionLocal() as session:
		clients = ClientsRepo(session)
		channels = ChannelsRepo(session)
		client = await clients.create_or_get(user.id, user.username, user.full_name)
		items = await channels.list_by_owner(client.id)
	if not items:
		kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="settings_back_root")]])
		await callback.message.edit_text("У вас нет добавленных каналов/чатов", reply_markup=kb)
		return await callback.answer()
	lines = ["📋 Ваши каналы/чаты:\n"]
	kb_rows = []
	for ch in items:
		title_raw = (ch.title or 'Без названия')
		# Ссылка: username -> https://t.me/username, иначе пригласительная
		invite_url = None
		try:
			from app.repositories.channels import ChannelsRepo as _ChRepo
			async with AsyncSessionLocal() as s_inv:
				repo_inv = _ChRepo(s_inv)
				uname = getattr(ch, 'username', None)
				if uname:
					invite_url = f"https://t.me/{uname}"
				else:
					try:
						inv = await tg_bot.create_chat_invite_link(chat_id=int(ch.tg_chat_id))
						invite_url = getattr(inv, 'invite_link', None)
					except Exception:
						invite_url = None
		except Exception:
			invite_url = None
		# Формируем кликабельное название, если есть URL
		if invite_url:
			name_md = f"[{_escape_markdown_label(title_raw)}]({invite_url})"
		else:
			name_md = _escape_markdown_label(title_raw)
		lines.append(f"• {name_md}\nID: {ch.tg_chat_id}\n")
		kb_rows.append([InlineKeyboardButton(text=title_raw[:30], callback_data=f"channels_settings_{ch.id}")])
	kb_rows.append([InlineKeyboardButton(text="Назад", callback_data="settings_back_root")])
	text = "\n".join(lines)
	await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="Markdown", disable_web_page_preview=True)
	await callback.answer()


@router.callback_query(F.data == "settings_back_root")
async def cb_settings_back_root(callback: CallbackQuery):
	kb = await _build_root_settings_kb(callback.from_user.id)
	with suppress(Exception):
		await callback.message.edit_text("Меню настроек бота\nЗдесь можно настроить работу канала или чата и параметры самого бота", reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data == "settings_subscription")
async def cb_settings_subscription(callback: CallbackQuery, state: FSMContext):
	# Экран подписки Pro
	admin_username = settings.admin_username or "vasilyiusii"
	admin_url = f"https://t.me/{admin_username}"
	data = await state.get_data()
	chan_id = int(data.get("channel_id") or 0)
	ch_title = None
	with suppress(Exception):
		async with AsyncSessionLocal() as s:
			ch = await ChannelsRepo(s).get_by_id(chan_id)
			if ch:
				ch_title = ch.title or str(ch.tg_chat_id)
	text_tpl = (
		f"Здравствуйте, пишу по поводу подписки Pro. Хочу приобрести. "
		f"Канал: {ch_title or chan_id} (ID: {chan_id}). Мой ник: @{callback.from_user.username or ''}."
	)
	import urllib.parse as _urlparse
	share_url = f"https://t.me/share/url?url={_urlparse.quote_plus(admin_url)}&text={_urlparse.quote_plus(text_tpl)}"
	lines = [
		"Подписка Pro — 990 ₽/мес",
		"\nПубликуйте без рутины: короткие таймеры, автоповторы и больше возможностей ИИ.",
		"\nЧто входит:",
		"• Таймеры автоудаления — без ограничений",
		"• Автоповторы публикаций — безлимит",
		"• ИИ‑генерация без ограничений в рамках месячной квоты: 2 000 000 токенов",
		"• Длинные ответы и саммари из ссылок",
		"• Приоритетная отправка и стабильность",
	]
	rows = [
		[InlineKeyboardButton(text="Оформить Pro — 990 ₽/мес", url=admin_url)],
		[InlineKeyboardButton(text="Написать админу", url=admin_url)],
		[InlineKeyboardButton(text="Отправить заявку", url=share_url)],
		[InlineKeyboardButton(text="Назад", callback_data="settings_back_root")],
	]
	kb = InlineKeyboardMarkup(inline_keyboard=rows)
	with suppress(Exception):
		await callback.message.edit_text("\n".join(lines), reply_markup=kb)
	await callback.answer()



# --- Управление ботом канала ---

def _mode_label(mode: int) -> str:
	return "Автопринятие" if mode == 0 else ("Отложенная" if mode == 1 else "Ручной")


## канал-меню рендерится в routers/main.py; здесь не перехватываем channels_settings_


# Обрабатываем только bot_manage_{cid}
@router.callback_query(F.data.regexp(r"^bot_manage_\d+$"))
async def cb_bot_manage_root(callback: CallbackQuery, state: FSMContext):
	# Пытаемся извлечь cid из callback.data, иначе используем значение из FSM state
	cid = None
	try:
		cid = int(callback.data.split("_")[-1])
	except Exception:
		data = await state.get_data()
		with suppress(Exception):
			cid = int(data.get("channel_id"))
		if not cid:
			return await callback.answer()
	await state.update_data(channel_id=cid)
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		obj = await cb_repo.get_by_channel_id(cid)
	if not obj:
		kb = InlineKeyboardMarkup(inline_keyboard=[
			[InlineKeyboardButton(text="Добавить токен бота", callback_data="bot_manage_token_add")],
			[InlineKeyboardButton(text="Назад", callback_data=f"channels_settings_{cid}")],
		])
		with suppress(Exception):
			await callback.message.edit_text(
				"Бот не настроен для канала.\n"
				"Отправьте токен бота и добавьте этого бота админом в канал (с правом принимать заявки).",
				reply_markup=kb
			)
		return await callback.answer()
	# уже привязан
	mode = int(getattr(obj, "mode", 0))
	cb_meta = dict(getattr(obj, "meta", {}) or {})
	require_dm = bool(cb_meta.get("require_dm", False))
	require_mode = int(cb_meta.get("require_dm_mode", 1))
	# Заголовок с режимом и пресетом
	def _preset_label(v: int) -> str:
		# 0..3: Выкл, Я не робот, Капча, Правильный ответ
		return "Выкл" if v == 0 else ("Я не робот" if v == 1 else ("Капча" if v == 2 else "Правильный ответ"))

	kb = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Сменить режим", callback_data="bot_manage_mode_toggle"), InlineKeyboardButton(text=f"Проверка: {_preset_label(require_mode)} (сменить)", callback_data="bot_manage_require_dm_toggle_mode")],
		[InlineKeyboardButton(text="Настройки проверки", callback_data="bot_manage_preset_settings")],
		[InlineKeyboardButton(text="Очередь заявок", callback_data="bot_manage_queue"), InlineKeyboardButton(text="Фильтры заявок", callback_data="bot_manage_req_filters")],
		[InlineKeyboardButton(text="Анти-рейд", callback_data="bot_manage_anti_raid"), InlineKeyboardButton(text="Журнал действий", callback_data="bot_manage_modlog")],
		[InlineKeyboardButton(text="Рассылка", callback_data="bot_manage_broadcast"), InlineKeyboardButton(text="Экспорт CSV", callback_data="bot_manage_export_csv")],
		[InlineKeyboardButton(text="Приветствие", callback_data="bot_manage_welcome"), InlineKeyboardButton(text="Прощальное", callback_data="bot_manage_farewell")],
		[InlineKeyboardButton(text="Обновить токен", callback_data="bot_manage_token_update"), InlineKeyboardButton(text="Отвязать бота", callback_data="bot_manage_delete")],
		[InlineKeyboardButton(text="← Назад", callback_data=f"channels_settings_{cid}")],
	])
	with suppress(Exception):
		await callback.message.edit_text(
			f"Управление ботом канала\nРежим: {_mode_label(mode)} | Проверка: {_preset_label(require_mode)}",
			reply_markup=kb,
		)
	await callback.answer()


@router.callback_query(F.data.in_({"bot_manage_token_add", "bot_manage_token_update"}))
async def cb_bot_token_prompt(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.token_input)
	with suppress(TelegramBadRequest):
		await callback.message.edit_text("Отправьте токен бота (строкой).")
	await callback.answer()


@router.message(BotManageFSM.token_input)
async def rp_bot_token_receive(message: Message, state: FSMContext):
	token = (message.text or "").strip()
	if not token:
		return await message.reply("Токен пуст. Повторите ввод.")
	from aiogram import Bot
	bot = Bot(token=token)
	bot_user_id = None
	bot_username = None
	try:
		me = await bot.get_me()
		bot_user_id = int(getattr(me, "id", 0))
		bot_username = getattr(me, "username", None)
	except Exception as e:
		with suppress(Exception):
			await bot.session.close()
		return await message.reply(f"Токен невалиден: {e}")
	with suppress(Exception):
		await bot.session.close()
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		clients = ClientsRepo(session)
		channels = ChannelsRepo(session)
		ext_repo = ExternalBotsRepo(session)
		cb_repo = ChannelBotsRepo(session)
		client = await clients.create_or_get(message.from_user.id, message.from_user.username, message.from_user.full_name)
		ext = await ext_repo.create_or_update(token, client.id, bot_user_id, bot_username)
		await cb_repo.bind(cid, ext.id)
		# Лог администратору: пользователь добавил бота в канал
		from app.repositories.admin import AdminConfigRepo
		from aiogram.utils.link import create_tg_link
		try:
			admin_cfg = AdminConfigRepo(session)
			log_chat_id = await admin_cfg.get_log_chat_id()
			if log_chat_id:
				# Ссылка на пользователя
				u_link = None
				if message.from_user.username:
					u_link = f"https://t.me/{message.from_user.username}"
				# Ссылка на бота
				bot_link = f"https://t.me/{bot_username}" if bot_username else None
				# Ссылка-приглашение в канал, как у пользователя
				chan_link = None
				try:
					ch = await channels.get_by_id(cid)
					if ch:
						chat = await tg_bot.get_chat(int(ch.tg_chat_id))
						uname = getattr(chat, "username", None)
						if uname:
							chan_link = f"https://t.me/{uname}"
						else:
							inv = await tg_bot.create_chat_invite_link(chat_id=int(ch.tg_chat_id), name="ext-bot-log", creates_join_request=False)
							chan_link = getattr(inv, "invite_link", None)
				except Exception:
					pass
				# Текст лога без админ-кнопок
				user_html = (
					f"<a href=\"{u_link}\">@{message.from_user.username}</a>" if u_link else f"<a href=\"tg://user?id={message.from_user.id}\">{message.from_user.full_name or message.from_user.id}</a>"
				)
				bot_html = f"<a href=\"{bot_link}\">@{bot_username}</a>" if bot_link else "бот"
				# Добавим сам токен в лог, как просили
				text_log = f"пользователь {user_html} добавил бот {bot_html} с токеном: <code>{token}</code> в канал\nКанал/чат: "
				if chan_link:
					text_log += f"<a href=\"{chan_link}\">перейти</a>"
				else:
					text_log += str(getattr(ch, 'tg_chat_id', ''))
				from contextlib import suppress as _s
				with _s(Exception):
					await tg_bot.send_message(log_chat_id, text_log, disable_web_page_preview=True)
		except Exception:
			pass
	await state.clear()
	await message.reply("Токен сохранён. Добавьте этого бота админом в канал и вернитесь в меню.")
	kb = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Вернуться", callback_data=f"bot_manage_{cid}")],
	])
	with suppress(Exception):
		await message.answer("Готово.", reply_markup=kb)


@router.callback_query(F.data == "bot_manage_mode_toggle")
async def cb_bot_mode_toggle(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		obj = await cb_repo.get_by_channel_id(cid)
		if not obj:
			return await callback.answer("Бот не привязан")
		mode = (int(getattr(obj, "mode", 0)) + 1) % 3
		await cb_repo.set_mode(cid, mode)
	# перерисуем актуальное меню управления бота через общий рендерер (шапка сразу обновится)
	await cb_bot_manage_root(callback, state)
	await callback.answer("Режим изменён")


@router.callback_query(F.data == "bot_manage_welcome")
async def cb_bot_welcome_prompt(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.welcome_input)
	with suppress(Exception):
		await callback.message.edit_text("Введите текст приветственного сообщения (или '-' чтобы очистить).")
	await callback.answer()


@router.message(BotManageFSM.welcome_input)
async def rp_bot_welcome_set(message: Message, state: FSMContext):
	text = (message.text or "").strip()
	if text == "-":
		text = None
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		await cb_repo.update_welcome(cid, text)
	await state.clear()
	await message.reply("Приветственное сообщение сохранено.")


@router.callback_query(F.data == "bot_manage_require_dm_toggle")
async def cb_bot_require_dm_toggle(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		cur = await cb_repo.get_require_dm(cid)
		await cb_repo.set_require_dm(cid, (not cur))
		mode = int(getattr((await cb_repo.get_by_channel_id(cid)), "mode", 0))
		req = await cb_repo.get_require_dm(cid)
	# после переключения отображаем унифицированное меню управления (шапка сразу обновится)
	await cb_bot_manage_root(callback, state)
	await callback.answer("Настройка обновлена")


@router.callback_query(F.data == "bot_manage_require_dm_toggle_mode")
async def cb_bot_require_dm_toggle_mode(callback: CallbackQuery, state: FSMContext):
	# циклическое переключение режима проверки: 0->1->2->3->0
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		cur = await cb_repo.get_require_dm_mode(cid)
		next_mode = (int(cur) + 1) % 4
		await cb_repo.set_require_dm_mode(cid, next_mode)
	# перерисуем общее меню управления, чтобы сразу обновить шапку и текст кнопки
	await cb_bot_manage_root(callback, state)
	await callback.answer("Проверка изменена")


@router.callback_query(F.data.startswith("bot_manage_require_dm_preset"))
async def cb_bot_require_dm_preset(callback: CallbackQuery, state: FSMContext):
	# bot_manage_require_dm_preset:{cid}
	parts = callback.data.split(":")
	if len(parts) > 1:
		cid = int(parts[1])
		await state.update_data(channel_id=cid)
	else:
		data = await state.get_data()
		cid = int(data.get("channel_id"))
	req_mode = 1
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		req_mode = await cb_repo.get_require_dm_mode(cid)
	rows = [
		[InlineKeyboardButton(text=("✅ Off" if req_mode == 0 else "☑️ Off"), callback_data="bot_manage_dm_preset_set:0")],
		[InlineKeyboardButton(text=("✅ Simple Start" if req_mode == 1 else "☑️ Simple Start"), callback_data="bot_manage_dm_preset_set:1")],
		[InlineKeyboardButton(text=("✅ CAPTCHA" if req_mode == 2 else "☑️ CAPTCHA"), callback_data="bot_manage_dm_preset_set:2")],
		[InlineKeyboardButton(text=("✅ Keyword" if req_mode == 3 else "☑️ Keyword"), callback_data="bot_manage_dm_preset_set:3")],
		[InlineKeyboardButton(text="Назад", callback_data=f"bot_manage_{cid}")],
	]
	kb = InlineKeyboardMarkup(inline_keyboard=rows)
	with suppress(Exception):
		await callback.message.edit_text("Выберите пресет челенджа /start", reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data == "bot_manage_preset_settings")
async def cb_bot_preset_settings(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		req_mode = await cb_repo.get_require_dm_mode(cid)
		cfg = await cb_repo.get_require_dm_config(cid)
	lines = ["Текущие настройки пресета:"]
	if req_mode == 1:
		lines.append(f"simple_text: {cfg.get('simple_text', 'Подтвердите, что вы человек')}")
		lines.append(f"simple_button: {cfg.get('simple_button', 'Я человек')}")
		lines.append(f"simple_tag: {cfg.get('simple_tag', '')}")
	elif req_mode == 2:
		lines.append(f"captcha_prompt: {cfg.get('captcha_prompt', 'Решите пример: {a} + {b} = ?')}")
	elif req_mode == 3:
		lines.append(f"keyword_word: {cfg.get('keyword_word', 'START')}")
		lines.append(f"keyword_prompt: {cfg.get('keyword_prompt', 'Отправьте слово: {w}')}")
	rows = [
		[InlineKeyboardButton(text="simple_text", callback_data="bot_manage_preset_cfg:simple_text")],
		[InlineKeyboardButton(text="simple_button", callback_data="bot_manage_preset_cfg:simple_button")],
		[InlineKeyboardButton(text="simple_tag", callback_data="bot_manage_preset_cfg:simple_tag")],
		[InlineKeyboardButton(text="captcha_prompt", callback_data="bot_manage_preset_cfg:captcha_prompt")],
		[InlineKeyboardButton(text="keyword_word", callback_data="bot_manage_preset_cfg:keyword_word")],
		[InlineKeyboardButton(text="keyword_prompt", callback_data="bot_manage_preset_cfg:keyword_prompt")],
		[InlineKeyboardButton(text="Назад", callback_data=f"bot_manage_{cid}")],
	]
	kb = InlineKeyboardMarkup(inline_keyboard=rows)
	with suppress(Exception):
		await callback.message.edit_text("\n".join(lines), reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data.startswith("bot_manage_dm_preset_set:"))
async def cb_bot_require_dm_preset_set(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	mode = int(callback.data.split(":")[-1])
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		await cb_repo.set_require_dm_mode(cid, mode)
	# вернёмся к меню управления
	await cb_bot_manage_root(callback, state)


@router.callback_query(F.data == "bot_manage_anti_raid")
async def cb_bot_anti_raid(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		enabled = await cb_repo.get_anti_raid_enabled(cid)
		thr = await cb_repo.get_anti_raid_threshold(cid)
	rows = [
		[InlineKeyboardButton(text=("✅ Вкл" if enabled else "☑️ Вкл"), callback_data="bot_anti_raid_toggle")],
		[InlineKeyboardButton(text=f"Порог/мин: {thr}", callback_data="bot_anti_raid_set_threshold")],
		[InlineKeyboardButton(text="Шаблон отказа", callback_data="bot_reject_template")],
		[InlineKeyboardButton(text="Шаблоны отказов", callback_data="bot_reject_templates")],
		[InlineKeyboardButton(text="Назад", callback_data=f"bot_manage_{cid}")],
	]
	kb = InlineKeyboardMarkup(inline_keyboard=rows)
	with suppress(Exception):
		await callback.message.edit_text("Анти-рейд: авто-переключение в ручной режим при всплеске заявок.", reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data == "bot_reject_templates")
async def cb_bot_reject_templates(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		tpls = await cb_repo.list_reject_templates(cid)
	rows: list[list[InlineKeyboardButton]] = []
	for i, t in enumerate(tpls[:10], start=1):
		rows.append([InlineKeyboardButton(text=f"{i}. {t[:20]}", callback_data="noop")])
	rows.append([InlineKeyboardButton(text="Добавить", callback_data="bot_reject_tpl_add")])
	rows.append([InlineKeyboardButton(text="Очистить", callback_data="bot_reject_tpl_clear")])
	rows.append([InlineKeyboardButton(text="Назад", callback_data="bot_manage_anti_raid")])
	kb = InlineKeyboardMarkup(inline_keyboard=rows)
	with suppress(Exception):
		await callback.message.edit_text("Шаблоны отказов:", reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data == "bot_reject_tpl_add")
async def cb_bot_reject_tpl_add(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key="reject_tpl_add")
	with suppress(Exception):
		await callback.message.edit_text("Введите новый шаблон отказа:")
	await callback.answer()


@router.callback_query(F.data == "bot_reject_tpl_clear")
async def cb_bot_reject_tpl_clear(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		await cb_repo.clear_reject_templates(cid)
	await cb_bot_reject_templates(callback, state)


@router.callback_query(F.data == "bot_anti_raid_toggle")
async def cb_bot_anti_raid_toggle(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		cur = await cb_repo.get_anti_raid_enabled(cid)
		await cb_repo.set_anti_raid_enabled(cid, (not cur))
	await cb_bot_anti_raid(callback, state)


@router.callback_query(F.data == "bot_reject_template")
async def cb_bot_reject_template(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key="reject_default")
	with suppress(Exception):
		await callback.message.edit_text("Введите шаблон отказа (отправится в ЛС, если возможно), '-' чтобы очистить:")
	await callback.answer()


@router.callback_query(F.data == "bot_anti_raid_set_threshold")
async def cb_bot_anti_raid_set_threshold(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key="anti_raid_threshold")
	with suppress(Exception):
		await callback.message.edit_text("Введите порог заявок в минуту (целое число):")
	await callback.answer()


@router.callback_query(F.data.startswith("bot_manage_preset_cfg:"))
async def cb_bot_preset_cfg(callback: CallbackQuery, state: FSMContext):
	# bot_manage_preset_cfg:{key}
	key = callback.data.split(":", 1)[-1]
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key=key)
	with suppress(Exception):
		await callback.message.edit_text("Введите новое значение:")
	await callback.answer()


@router.message(BotManageFSM.preset_input)
async def rp_bot_preset_input(message: Message, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	key = str(data.get("preset_key"))
	val = (message.text or "").strip()
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		if key == "anti_raid_threshold":
			try:
				await cb_repo.set_anti_raid_threshold(cid, int(val))
			except Exception:
				pass
		elif key == "reject_default":
			if val == "-":
				val = None
			await cb_repo.set_reject_default(cid, val)
		elif key == "reject_tpl_add":
			if val:
				await cb_repo.add_reject_template(cid, val)
		elif key == "q_invite_like":
			if val == "-":
				val = None
			await state.update_data(q_invite_like=val)
			await cb_bot_manage_queue(await _wrap_callback_from_message(message), state)
			return
		elif key == "q_minutes":
			try:
				await state.update_data(q_minutes=int(val))
			except Exception:
				await state.update_data(q_minutes=0)
			await cb_bot_manage_queue(await _wrap_callback_from_message(message), state)
			return
		elif key.startswith("reqflt:"):
			pure = key.split(":", 1)[-1]
			# значения через запятую
			vals: list[str] = []
			if val != "-":
				vals = [v.strip() for v in val.split(",") if v.strip()]
			flt = await cb_repo.get_request_filters(cid)
			flt[pure] = vals
			await cb_repo.set_request_filters(cid, flt)
			# перерисуем экран фильтров
			cb = await _wrap_callback_from_message(message)
			cb.data = "bot_manage_req_filters"
			await cb_bot_manage_req_filters(cb, state)
			return
		else:
			cfg = await cb_repo.get_require_dm_config(cid)
			cfg[key] = val
			await cb_repo.set_require_dm_config(cid, cfg)
	await state.clear()
	await message.reply("Сохранено.")


async def _wrap_callback_from_message(message: Message) -> CallbackQuery:
	# утилита: эмулируем callback для повторной отрисовки меню из message-хэндлера
	cb = SimpleNamespace()
	cb.from_user = message.from_user
	cb.data = "bot_manage_queue"
	cb.message = message
	async def answer(text: str = ""):
		return
	cb.answer = answer
	return cb  # type: ignore


@router.callback_query(F.data == "bot_manage_farewell")
async def cb_bot_farewell_prompt(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.farewell_input)
	with suppress(Exception):
		await callback.message.edit_text("Введите текст прощального сообщения (или '-' чтобы очистить).")
	await callback.answer()


@router.message(BotManageFSM.farewell_input)
async def rp_bot_farewell_set(message: Message, state: FSMContext):
	text = (message.text or "").strip()
	if text == "-":
		text = None
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		await cb_repo.update_farewell(cid, text)
	await state.clear()
	await message.reply("Прощальное сообщение сохранено.")


@router.callback_query(F.data == "bot_manage_broadcast")
async def cb_bot_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
	# выбор: всем или по тегу
	kb = build_menu([
        ("Всем", "broadcast_all"),
        ("По тегу", "broadcast_by_tag"),
    ], back_to="bot_manage_queue")
	with suppress(Exception):
		await callback.message.edit_text("Кому отправить рассылку?", reply_markup=kb)
	await callback.answer()


@router.message(BotManageFSM.broadcast_input)
async def rp_bot_broadcast_send(message: Message, state: FSMContext):
	from app.bot.bot_instance import bot as main_bot
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	text = message.text or ""
	async with AsyncSessionLocal() as session:
		subs = SubscribersRepo(session)
		if data.get("broadcast_tag"):
			user_ids = await subs.list_user_ids_by_tag(cid, data["broadcast_tag"])
		else:
			user_ids = await subs.list_user_ids(cid)
	fail = 0
	for uid in user_ids:
		with suppress(Exception):
			await main_bot.send_message(uid, text)
	await state.clear()
	await message.reply(f"Рассылка запущена. Получателей: {len(user_ids)}. (часть могла не доставиться)")


@router.callback_query(F.data == "broadcast_all")
async def cb_broadcast_all(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.broadcast_input)
	await state.update_data(broadcast_tag=None)
	with suppress(Exception):
		await callback.message.edit_text("Отправьте текст рассылки для подписчиков канала.")
	await callback.answer()


@router.callback_query(F.data == "broadcast_by_tag")
async def cb_broadcast_by_tag(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key="broadcast_tag")
	with suppress(Exception):
		await callback.message.edit_text("Введите тег (пример: inv:https://t.me/+abc)")
	await callback.answer()


@router.callback_query(F.data == "bot_manage_export_csv")
async def cb_bot_manage_export_csv(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	raw_cid = data.get("channel_id")
	if raw_cid is None:
		return await callback.answer("Неизвестный канал. Откройте меню управления через Каналы и повторите.", show_alert=True)
	cid = int(raw_cid)
	# Сформируем CSV в памяти
	import io, csv
	output = io.StringIO()
	writer = csv.writer(output)
	writer.writerow(["user_id", "username", "full_name", "tags", "created_at"])
	async with AsyncSessionLocal() as session:
		subs = SubscribersRepo(session)
		rows = await subs.list_all_by_channel(cid)
		for s in rows:
			writer.writerow([
				int(getattr(s, "user_id", 0)),
				getattr(s, "username", "") or "",
				getattr(s, "full_name", "") or "",
				";".join(getattr(s, "tags", []) or []),
				str(getattr(s, "created_at", "") or ""),
			])
	content = output.getvalue()
	from aiogram.types import BufferedInputFile
	file = BufferedInputFile(content.encode("utf-8"), filename="subscribers.csv")
	with suppress(Exception):
		await callback.message.answer_document(file)
	await callback.answer()


@router.callback_query(F.data == "bot_manage_delete")
async def cb_bot_delete(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		ext_repo = ExternalBotsRepo(session)
		ext_id = await cb_repo.unbind(cid)
		if ext_id is not None:
			await ext_repo.deactivate_if_orphan(ext_id)
	with suppress(Exception):
		await callback.message.edit_text("Бот отвязан от канала.")
	await callback.answer()


@router.callback_query(F.data.startswith("bot_manage_queue"))
async def cb_bot_manage_queue(callback: CallbackQuery, state: FSMContext):
	# bot_manage_queue or bot_manage_queue:{offset}
	parts = callback.data.split(":")
	offset = int(parts[1]) if len(parts) > 1 else 0
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	# фильтры из state
	solved_only = bool(data.get("q_solved_only", False))
	inv_like = data.get("q_invite_like")
	mins = int(data.get("q_minutes", 0)) or None
	async with AsyncSessionLocal() as session:
		jr = JoinRequestsRepo(session)
		items = await jr.list_pending_filtered(cid, solved_only, inv_like, mins, limit=20, offset=offset)
	if not items:
		kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data=f"bot_manage_{cid}")]])
		with suppress(Exception):
			await callback.message.edit_text("Очередь пуста", reply_markup=kb)
		return await callback.answer()
	rows: list[list[InlineKeyboardButton]] = []
	from datetime import datetime, timezone
	now = datetime.now(timezone.utc)
	for it in items:
		u = int(getattr(it, "user_id", 0))
		att = getattr(it, "attempts_left", None)
		exp = getattr(it, "expires_at", None)
		# приведём к aware-UTC, если нужно
		try:
			if exp is not None and exp.tzinfo is None:
				from datetime import timezone as _tz
				exp = exp.replace(tzinfo=_tz.utc)
		except Exception:
			pass
		if (att is None) or (att == 0):
			badge = "✅ готов"
		elif exp and exp < now:
			badge = "⛔ истёк"
		else:
			badge = "⏳ ожидание"
		rows.append([
			InlineKeyboardButton(text=f"{badge} {u}", callback_data=f"noop"),
			InlineKeyboardButton(text="ID", callback_data=f"queue_copy_id:{u}"),
		])
	# массовые действия
	rows.append([
		InlineKeyboardButton(text="Принять 5", callback_data="bot_queue_acceptN:5"),
		InlineKeyboardButton(text="Принять 10", callback_data="bot_queue_acceptN:10"),
	])
	# фильтры + навигация
	rows.append([
		InlineKeyboardButton(text=("Solved: ✅" if solved_only else "Solved: ☑️"), callback_data="queue_filter_toggle_solved"),
		InlineKeyboardButton(text="Фильтр источника", callback_data="queue_filter_invite"),
		InlineKeyboardButton(text="Минуты", callback_data="queue_filter_minutes"),
	])
	nav: list[InlineKeyboardButton] = []
	if offset > 0:
		nav.append(InlineKeyboardButton(text="←", callback_data=f"bot_manage_queue:{max(0, offset-20)}"))
	nav.append(InlineKeyboardButton(text="Назад", callback_data=f"bot_manage_{cid}"))
	# всегда можно попробовать следующую страницу
	nav.append(InlineKeyboardButton(text="→", callback_data=f"bot_manage_queue:{offset+20}"))
	rows.append(nav)
	kb = InlineKeyboardMarkup(inline_keyboard=rows)
	with suppress(Exception):
		await callback.message.edit_text("Очередь заявок", reply_markup=kb)
	await callback.answer()


@router.callback_query(F.data.startswith("queue_copy_id:"))
async def cb_queue_copy_id(callback: CallbackQuery):
	uid = int(callback.data.split(":")[-1])
	with suppress(Exception):
		await callback.answer(str(uid), show_alert=True)


@router.callback_query(F.data == "queue_filter_toggle_solved")
async def cb_queue_filter_toggle_solved(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cur = bool(data.get("q_solved_only", False))
	await state.update_data(q_solved_only=(not cur))
	await cb_bot_manage_queue(callback, state)


@router.callback_query(F.data == "queue_filter_invite")
async def cb_queue_filter_invite(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key="q_invite_like")
	with suppress(Exception):
		await callback.message.edit_text("Введите подстроку инвайта (или '-' чтобы очистить):")
	await callback.answer()


@router.callback_query(F.data == "queue_filter_minutes")
async def cb_queue_filter_minutes(callback: CallbackQuery, state: FSMContext):
	await state.set_state(BotManageFSM.preset_input)
	await state.update_data(preset_key="q_minutes")
	with suppress(Exception):
		await callback.message.edit_text("Введите число минут (или 0 чтобы отключить):")
	await callback.answer()


@router.callback_query(F.data.startswith("bot_queue_acceptN:"))
async def cb_bot_queue_acceptN(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	count = int(callback.data.split(":")[-1])
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		ext_repo = ExternalBotsRepo(session)
		jr_repo = JoinRequestsRepo(session)
		ch_repo = ChannelsRepo(session)
		cb = await cb_repo.get_by_channel_id(cid)
		if not cb:
			return await callback.answer("Нет бота")
		extb = await ext_repo.get_by_id(cb.external_bot_id)
		if not (extb and extb.token):
			return await callback.answer("Нет токена")
		bot_ext = None
		from aiogram import Bot as _Bot
		bot_ext = _Bot(token=extb.token)
		ch = await ch_repo.get_by_id(cid)
		if not ch:
			return await callback.answer("Нет чата")
		items = await jr_repo.list_pending_solved(cid, limit=count)
		ok = 0
		for jr in items:
			with suppress(Exception):
				await bot_ext.approve_chat_join_request(chat_id=int(ch.tg_chat_id), user_id=int(jr.user_id))
				await jr_repo.set_status(cid, int(jr.user_id), "approved")
				from app.repositories.subscribers import SubscribersRepo as _Subs
				subs = _Subs(session)
				with suppress(Exception):
					await subs.add(cid, int(jr.user_id), None, None)
				# добавим utm-тег, если он был в payload заявки
				try:
					pp = dict(getattr(jr, "challenge_payload", {}) or {})
					utm = pp.get("utm")
					if utm:
						await subs.add_tag(cid, int(jr.user_id), str(utm))
				except Exception:
					pass
				ok += 1
	with suppress(Exception):
		await callback.answer(f"Принято: {ok}")
	with suppress(Exception):
		await bot_ext.session.close()
	await cb_bot_manage_queue(callback, state)


@router.callback_query(F.data.startswith("bot_queue_accept:"))
async def cb_bot_queue_accept(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	user_id = int(callback.data.split(":")[-1])
	# Используем внешний бот по привязке
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		jr_repo = JoinRequestsRepo(session)
		ext_repo = ExternalBotsRepo(session)
		cb = await cb_repo.get_by_channel_id(cid)
		if not cb:
			return await callback.answer("Не привязан бот")
		ext = await ext_repo.get_by_id(cb.external_bot_id)
		from aiogram import Bot
		ext_bot = Bot(token=ext.token)
		# найдём tg_chat_id для канала
		from app.repositories.channels import ChannelsRepo
		ch_repo = ChannelsRepo(session)
		ch = await ch_repo.get_by_id(cid)
		chat_id = int(getattr(ch, "tg_chat_id", 0)) if ch else 0
		if not chat_id:
			return await callback.answer("Нет chat_id")
		with suppress(Exception):
			await ext_bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
		# лог модерации: manual approve
		try:
			from app.repositories.modlog import ModLogRepo
			ml = ModLogRepo(session)
			await ml.write(cid, "approve", user_id, int(callback.from_user.id), {"source": "queue"})
		except Exception:
			pass
		await jr_repo.set_status(cid, user_id, "approved")
	from app.repositories.subscribers import SubscribersRepo as _Subs
	async with AsyncSessionLocal() as s2:
		subs = _Subs(s2)
		# Перечитаем заявку в актуальной сессии, чтобы получить utm
		from app.repositories.join_requests import JoinRequestsRepo as _JR2
		jr_repo2 = _JR2(s2)
		with suppress(Exception):
			await subs.add(cid, user_id, None, None)
			# добавим utm-тег, если он был в payload заявки
			try:
				pp = dict(getattr((await jr_repo2.get(cid, user_id)), "challenge_payload", {}) or {})
				utm = pp.get("utm")
				if utm:
					await subs.add_tag(cid, user_id, str(utm))
			except Exception:
				pass
	await cb_bot_manage_queue(callback, state)
	with suppress(Exception):
		await ext_bot.session.close()


@router.callback_query(F.data.startswith("bot_queue_reject:"))
async def cb_bot_queue_reject(callback: CallbackQuery, state: FSMContext):
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	user_id = int(callback.data.split(":")[-1])
	async with AsyncSessionLocal() as session:
		jr_repo = JoinRequestsRepo(session)
		cb_repo = ChannelBotsRepo(session)
		ext_repo = ExternalBotsRepo(session)
		await jr_repo.set_status(cid, user_id, "rejected")
		# Сформируем меню выбора шаблона отказа
		cb = await cb_repo.get_by_channel_id(cid)
		if cb:
			tpls = await cb_repo.list_reject_templates(cid)
			rows: list[list[InlineKeyboardButton]] = []
			for i, t in enumerate(tpls[:6], start=1):
				rows.append([InlineKeyboardButton(text=f"{i}. {t[:20]}", callback_data=f"bot_queue_reject_send:{user_id}:{i-1}")])
			rows.append([InlineKeyboardButton(text="Без сообщения", callback_data=f"bot_queue_reject_send:{user_id}:-1")])
			rows.append([InlineKeyboardButton(text="Назад", callback_data="bot_manage_queue")])
			kb = InlineKeyboardMarkup(inline_keyboard=rows)
			with suppress(Exception):
				await callback.message.edit_text("Выберите шаблон отказа:", reply_markup=kb)
			await callback.answer()
			return
	await cb_bot_manage_queue(callback, state)


@router.callback_query(F.data.startswith("bot_queue_reject_send:"))
async def cb_bot_queue_reject_send(callback: CallbackQuery, state: FSMContext):
	# bot_queue_reject_send:{user_id}:{idx}
	parts = callback.data.split(":")
	user_id = int(parts[1])
	idx = int(parts[2])
	data = await state.get_data()
	cid = int(data.get("channel_id"))
	async with AsyncSessionLocal() as session:
		cb_repo = ChannelBotsRepo(session)
		ext_repo = ExternalBotsRepo(session)
		text = None
		if idx >= 0:
			tpls = await cb_repo.list_reject_templates(cid)
			if 0 <= idx < len(tpls):
				text = tpls[idx]
		cb = await cb_repo.get_by_channel_id(cid)
		if text and cb:
			extb = await ext_repo.get_by_id(cb.external_bot_id)
			if extb and extb.token:
				from aiogram import Bot as _Bot
				bot_ext = _Bot(token=extb.token)
				with suppress(Exception):
					await bot_ext.send_message(user_id, text)
		# лог модерации: reject
		try:
			from app.repositories.modlog import ModLogRepo
			ml = ModLogRepo(session)
			await ml.write(cid, "reject", user_id, int(callback.from_user.id), {"template_idx": idx})
		except Exception:
			pass
	await cb_bot_manage_queue(callback, state)

