from pyrogram import Client
from app.core.config import settings
import os


# Поддержка session_string через переменную окружения USERBOT_SESSION (если есть)
SESSION_STRING = os.getenv("USERBOT_SESSION")

if SESSION_STRING:
	app = Client(name="userbot", api_id=settings.api_id, api_hash=settings.api_hash, session_string=SESSION_STRING)
else:
	# Фолбэк на файл-сессию ("userbot.session" в рабочем каталоге проекта)
	app = Client(name="userbot", api_id=settings.api_id, api_hash=settings.api_hash)


