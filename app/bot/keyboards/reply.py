from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonRequestChat, ChatAdministratorRights


def main_menu_kb() -> ReplyKeyboardMarkup:
	return ReplyKeyboardMarkup(
		keyboard=[
			[KeyboardButton(text="Добавить канал/чат"), KeyboardButton(text="Создать пост")],
			[KeyboardButton(text="Контент план")],
			[KeyboardButton(text="Редактировать пост"), KeyboardButton(text="Черновик")],
			[KeyboardButton(text="Настройки")],
		],
		resize_keyboard=True,
		one_time_keyboard=False,
		is_persistent=True,
	)


def add_channel_kb() -> ReplyKeyboardMarkup:
	# Один шаг: обе кнопки сразу с request_chat и согласованными правами
	# Профиль КАНАЛ (как в pick_channel_request_kb)
	channel_user_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=True,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=True,
		can_edit_messages=True,
		can_pin_messages=False,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	channel_bot_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=False,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=True,
		can_edit_messages=True,
		can_pin_messages=False,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	btn_channel = KeyboardButton(
		text="Канал",
		request_chat=KeyboardButtonRequestChat(
			request_id=21,
			chat_is_channel=True,
			chat_is_created=False,
			user_administrator_rights=channel_user_rights,
			bot_administrator_rights=channel_bot_rights,
		),
	)
	# Профиль ЧАТ (как в обновлённом pick_group_request_kb)
	group_user_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=True,
		can_promote_members=True,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=False,
		can_edit_messages=False,
		can_pin_messages=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	group_bot_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=True,
		can_promote_members=False,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=False,
		can_edit_messages=False,
		can_pin_messages=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	btn_group = KeyboardButton(
		text="Чат",
		request_chat=KeyboardButtonRequestChat(
			request_id=22,
			chat_is_channel=False,
			chat_is_created=False,
			user_administrator_rights=group_user_rights,
			bot_administrator_rights=group_bot_rights,
		),
	)
	return ReplyKeyboardMarkup(
		keyboard=[[btn_channel, btn_group], [KeyboardButton(text="Главное меню")]],
		resize_keyboard=True,
		one_time_keyboard=False,
		is_persistent=True,
	)


def pick_channel_request_kb() -> ReplyKeyboardMarkup:
	# user права для КАНАЛА
	user_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=True,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=True,
		can_edit_messages=True,
		can_pin_messages=False,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	# bot права для КАНАЛА (как просили)
	bot_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=False,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=True,
		can_edit_messages=True,
		can_pin_messages=False,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	btn = KeyboardButton(
		text="Выбрать канал",
		request_chat=KeyboardButtonRequestChat(
			request_id=11,
			chat_is_channel=True,
			chat_is_created=False,
			user_administrator_rights=user_rights,
			bot_administrator_rights=bot_rights,
		),
	)
	return ReplyKeyboardMarkup(
		keyboard=[[btn], [KeyboardButton(text="Главное меню")]],
		resize_keyboard=True,
		one_time_keyboard=False,
		is_persistent=True,
	)


def pick_group_request_kb() -> ReplyKeyboardMarkup:
	# user права для ЧАТА/ГРУППЫ
	user_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=True,
		can_promote_members=True,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=False,
		can_edit_messages=False,
		can_pin_messages=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	# bot права для ЧАТА/ГРУППЫ (как просили)
	bot_rights = ChatAdministratorRights(
		is_anonymous=False,
		can_manage_chat=False,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=True,
		can_promote_members=False,
		can_change_info=True,
		can_invite_users=True,
		can_post_messages=False,
		can_edit_messages=False,
		can_pin_messages=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
	)
	btn = KeyboardButton(
		text="Выбрать чат",
		request_chat=KeyboardButtonRequestChat(
			request_id=13,
			chat_is_channel=False,
			chat_is_created=False,
			user_administrator_rights=user_rights,
			bot_administrator_rights=bot_rights,
		),
	)
	return ReplyKeyboardMarkup(
		keyboard=[[btn], [KeyboardButton(text="Главное меню")]],
		resize_keyboard=True,
		one_time_keyboard=False,
		is_persistent=True,
	)