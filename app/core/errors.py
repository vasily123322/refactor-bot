from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import Update
from typing import Callable, Dict, Any, Awaitable
from loguru import logger


class ErrorsMiddleware(BaseMiddleware):
	"""Глобальная обработка ошибок: лог + мягкий ответ пользователю."""

	async def __call__(self, handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]], event: Update, data: Dict[str, Any]) -> Any:
		try:
			return await handler(event, data)
		except Exception as e:
			try:
				user_id = None
				chat_id = None
				if hasattr(event, "message") and event.message:
					user_id = getattr(getattr(event.message, "from_user", None), "id", None)
					chat_id = getattr(getattr(event.message, "chat", None), "id", None)
				elif hasattr(event, "callback_query") and event.callback_query:
					user_id = getattr(getattr(event.callback_query, "from_user", None), "id", None)
					chat_id = getattr(getattr(getattr(event.callback_query, "message", None), "chat", None), "id", None)
				logger.bind(user_id=user_id, chat_id=chat_id).exception(f"Unhandled error: {e}")
			except Exception:
				logger.exception("Unhandled error (logging failed)")
			# Пытаемся мягко уведомить пользователя, если это message/callback
			try:
				if getattr(event, "message", None):
					await event.message.answer("Произошла ошибка. Попробуйте ещё раз.")
				elif getattr(event, "callback_query", None):
					await event.callback_query.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
			except Exception:
				pass
			return None



