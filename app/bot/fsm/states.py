from aiogram.fsm.state import StatesGroup, State


class PostFSM(StatesGroup):
	edit_pick = State()
	content = State()
	caption = State()
	buttons = State()
	preview = State()
	defer_time_input = State()
	ai_topic_input = State()  # Ввод темы для генерации ИИ
	ai_link_input = State()   # Ввод ссылки для саммари
	ai_improve_input = State() # Ввод инструкции для улучшения текста
	# New quick create card
	create_card = State()
	ad_name_input = State()  # Имя рекламодателя для новой брони


class SettingsFSM(StatesGroup):
	root = State()
	tz_search_input = State()
	autosign_input = State()
	split_add_input = State()
	split_remove_input = State()
	forbidden_input = State()  # Ввод запрещённых слов (список)
	source_input = State()  # Ввод источника для ИИ
	custom_system_input = State()  # Ввод системного промпта ИИ
	custom_user_input = State()    # Ввод шаблона пользователя ИИ


class ApplicationsFSM(StatesGroup):
	root = State()


class GrabberFSM(StatesGroup):
	root = State()
	add_source_input = State()
	remove_index_input = State()


class BotManageFSM(StatesGroup):
	root = State()
	token_input = State()
	welcome_input = State()
	farewell_input = State()
	broadcast_input = State()
	preset_input = State()

