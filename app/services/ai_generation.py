"""
Сервис для генерации контента через OpenRouter API.
"""
import httpx
from loguru import logger
from bs4 import BeautifulSoup
from app.core.config import settings
from app.repositories.ai_settings import ChannelAISettingsRepo, AIPresetsRepo
from app.repositories.custom_prompts import CustomSystemPromptsRepo
from app.repositories.conversations import ConversationsRepo
from sqlalchemy.ext.asyncio import AsyncSession
import json
from app.repositories.channels import ChannelsRepo
from app.domain.models import Client


class AIGenerationService:
	"""Сервис генерации контента через LLM."""
	
	def __init__(self, session: AsyncSession):
		self.session = session
		self.ai_repo = ChannelAISettingsRepo(session)
		self.presets_repo = AIPresetsRepo(session)
		self.conv_repo = ConversationsRepo(session)
		self.custom_repo = CustomSystemPromptsRepo(session)

	async def clear_history(self, *, user_id: int, prompt_key: str | None = None, channel_id: int | None = None) -> int:
		"""Очистить историю диалога.
		Если указан prompt_key — удалит только один диалог пользователя.
		Если указан channel_id — удалит все диалоги, привязанные к каналу.
		Иначе — все диалоги пользователя.
		Возвращает количество удалённых диалогов.
		"""
		if prompt_key:
			return await self.conv_repo.delete_by_user_and_prompt_key(user_id=user_id, prompt_key=prompt_key)
		if channel_id is not None:
			return await self.conv_repo.delete_all_by_channel(channel_id)
		return await self.conv_repo.delete_all_by_user(user_id)
	
	async def generate_text(self, channel_id: int, topic: str, **extra_vars) -> dict:
		"""
		Генерация текста для поста.
		
		Args:
			channel_id: ID канала
			topic: Тема поста
			**extra_vars: Дополнительные переменные для промпта (brand, audience, cta и т.д.)
		
		Returns:
			dict с ключами: success, text, tokens_used, error
		"""
		# Получаем настройки ИИ канала
		ai_settings = await self.ai_repo.get_or_create(channel_id)
		
		if not ai_settings.enabled:
			return {"success": False, "error": "ИИ отключен для этого канала", "text": None, "tokens_used": 0}
		
		# Эффективные лимиты по плану (Free/Pro) и предзапросная проверка
		eff_day, eff_month, req_cap = await self._effective_limits(ai_settings, channel_id)
		# В Pro дневной лимит отключён; для Free — применим, если задан
		if eff_day is not None and int(ai_settings.tokens_used_day or 0) >= int(eff_day):
			return {"success": False, "error": "Превышен дневной лимит токенов", "text": None, "tokens_used": 0}
		if eff_month is not None and int(ai_settings.tokens_used_month or 0) >= int(eff_month):
			return {"success": False, "error": "Превышен месячный лимит токенов", "text": None, "tokens_used": 0}
		
		# Получаем промпт (из пресета или кастомный)
		system_prompt, user_prompt = await self._build_prompts(ai_settings, topic, extra_vars)
		
		# Применим кэп на запрос
		if (ai_settings.max_tokens or 0) > int(req_cap):
			ai_settings.max_tokens = int(req_cap)
		# Выбор модели: базовая или пер-режимная (from_scratch для обычной генерации)
		chosen_model = self._pick_model(ai_settings, mode="from_scratch")
		base_url, api_key = self._resolve_model_credentials(chosen_model)

		# Память диалога (если переданы user_id и prompt_key в extra_vars)
		user_id = extra_vars.get("user_id")
		prompt_key = extra_vars.get("prompt_key")
		if user_id and prompt_key:
			messages = [{"role": "system", "content": system_prompt}]
			conv_id = await self.conv_repo.get_or_create(user_id=int(user_id), prompt_key=str(prompt_key), channel_id=channel_id)
			budget = max(512, int((ai_settings.max_tokens or 2000) * 0.7))
			summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
			if summary:
				messages.append({"role": "system", "content": f"Сводка контекста:\n{summary}"})
			recent = await self.conv_repo.list_recent_by_tokens(conv_id, max_tokens=budget - int(summary_tokens or 0))
			messages.extend(recent)
			messages.append({"role": "user", "content": user_prompt})
			await self.conv_repo.append(conv_id, role="user", content=user_prompt, tokens=0)
			result = await self._call_openrouter_messages(
				messages,
				model=chosen_model,
				temperature=ai_settings.temperature,
				top_p=ai_settings.top_p,
				max_tokens=ai_settings.max_tokens,
				base_url=base_url,
				api_key=api_key,
			)
			if result.get("success"):
				await self.conv_repo.append(conv_id, role="assistant", content=result.get("text") or "", tokens=0)
		else:
			# Генерация без памяти
			result = await self._call_openrouter(
				system_prompt=system_prompt,
				user_prompt=user_prompt,
				model=chosen_model,
				temperature=ai_settings.temperature,
				top_p=ai_settings.top_p,
				max_tokens=ai_settings.max_tokens,
				base_url=base_url,
				api_key=api_key
			)
		
		if not result["success"]:
			return result
		
		# Обновляем счётчик токенов
		tokens_used = result.get("tokens_used", 0)
		if tokens_used > 0:
			await self.ai_repo.increment_tokens(channel_id, tokens_used)
			# Нотификация 80% месячной квоты
			try:
				is_pro = await self._is_pro_channel(channel_id)
				if is_pro:
					settings_now = await self.ai_repo.get_or_create(channel_id)
					day_limit, month_limit, _ = await self._effective_limits(settings_now, channel_id)
					if month_limit:
						used = int(getattr(settings_now, "tokens_used_month", 0) or 0)
						pct = (used / int(month_limit)) if month_limit else 0
						if pct >= 0.8 and pct < 1.0:
							# Отправим владельцу канала предложение пополнить токены
							from app.repositories.channels import ChannelsRepo as _ChRepo
							from app.domain.models import Client as _Client
							from app.bot.bot_instance import bot as _bot
							async with self.session.bind.connect() as _:
								pass
							ch = await _ChRepo(self.session).get_by_id(channel_id)
							if ch:
								owner = await self.session.get(_Client, int(getattr(ch, "owner_id", 0)))
								uid = int(getattr(owner, "tg_user_id", 0)) if owner else 0
								if uid:
									text = (
										"⚠️ 80% месячной квоты ИИ израсходовано.\n\n"
										"Можно пополнить токены:\n"
										"• 500k — 299 ₽\n"
										"• 1M — 499 ₽\n"
										"• 2M — 899 ₽\n\n"
										"Неиспользованные доп. токены не сгорают и переносятся в следующий месяц с Pro или Free."
									)
									# Кнопки: Написать админу / Отправить заявку (с префиллом)
									from app.core.config import settings as _cfg
									admin_username = _cfg.admin_username or "vasilyiusii"
									admin_url = f"https://t.me/{admin_username}"
									import urllib.parse as _urlparse
									share_text = (
										"Здравствуйте! Хочу пополнить доп. токены (500k/1M/2M). "
										f"Канал ID: {getattr(ch, 'tg_chat_id', channel_id)}."
									)
									share_url = f"https://t.me/share/url?url={_urlparse.quote_plus(admin_url)}&text={_urlparse.quote_plus(share_text)}"
									from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
									kb = InlineKeyboardMarkup(inline_keyboard=[
										[InlineKeyboardButton(text="Написать админу", url=admin_url)],
										[InlineKeyboardButton(text="Отправить заявку", url=share_url)],
									])
									from contextlib import suppress as _s
									with _s(Exception):
										await _bot.send_message(uid, text, reply_markup=kb)
									# Дублируем событие в лог‑канал администратора
									try:
										from app.repositories.admin import AdminConfigRepo as _AdminRepo
										log_chat_id = await _AdminRepo(self.session).get_log_chat_id()
										if log_chat_id:
											# Ссылка на канал
											chan_link = None
											try:
												chat = await _bot.get_chat(int(getattr(ch, "tg_chat_id", 0))) if ch else None
												uname = getattr(chat, "username", None) if chat else None
												if uname:
													chan_link = f"https://t.me/{uname}"
											except Exception:
												chan_link = None
											pct_int = int(pct * 100)
											log_text = (
												f"AI tokens 80%: channel={(chan_link or getattr(ch, 'tg_chat_id', channel_id))} "
												f"used={used}/{int(month_limit)} (~{pct_int}%)"
											)
											with _s(Exception):
												await _bot.send_message(int(log_chat_id), log_text, disable_web_page_preview=True)
									except Exception:
										pass
			except Exception:
				pass
		
		# Пост-обработка: применяем модерацию, если включена
		text = result["text"]
		if ai_settings.moderation_enabled:
			text = await self._moderate_text(text, ai_settings)
		
		return {
			"success": True,
			"text": text,
			"tokens_used": tokens_used,
			"error": None
		}

	# --- Unified pipeline helpers ---
	async def build_prompt(self, channel_id: int, *, mode: str, topic: str = "", original_text: str = "", url: str = "", instruction: str | None = None, extra: dict | None = None) -> tuple[dict, str, str, str]:
		"""Собрать контекст и промпты под конкретный режим.

		Возвращает кортеж: (ai_settings_dict, chosen_model, system_prompt, user_prompt).
		"""
		extra = extra or {}
		ai_settings = await self.ai_repo.get_or_create(channel_id)
		if mode == "from_link":
			# для ссылок instruction берём из стандартной карты
			mode_instructions = {
				"summary": "Создай краткое саммари (резюме) этой статьи для Telegram-поста.",
				"rewrite": "Перепиши эту статью своими словами, сохраняя ключевые идеи.",
				"paraphrase": "Перефразируй эту статью, сделав её более понятной.",
			}
			inst = instruction or mode_instructions.get(extra.get("link_mode") or "summary", mode_instructions["summary"])
			system_prompt, user_prompt = await self._build_prompts(ai_settings, topic="", extra_vars={"article_text": extra.get("article_text", ""), "source_url": url, "instruction": inst, "force_custom": bool(extra.get("force_custom", False))})
			chosen_model = self._pick_model(ai_settings, mode=extra.get("link_mode") or "summary")
		elif mode == "improve":
			system_prompt, user_prompt = await self._build_prompts(ai_settings, topic="", extra_vars={"original_text": original_text, "instruction": (instruction or "улучши"), "force_custom": bool(extra.get("force_custom", False))})
			chosen_model = self._pick_model(ai_settings, mode="rewrite")
		else:
			# default: from_scratch
			system_prompt, user_prompt = await self._build_prompts(ai_settings, topic, extra)
			chosen_model = self._pick_model(ai_settings, mode="from_scratch")
		return ai_settings.__dict__, chosen_model, system_prompt, user_prompt

	async def generate_with_model(self, *, ai_settings: dict, model: str, system_prompt: str, user_prompt: str, user_id: int | None = None, prompt_key: str | None = None) -> dict:
		"""Единый вызов LLM с учётом памяти диалога и апдейта токенов."""
		# восстановим объект-подобный интерфейс для полей
		class _Obj:
			def __init__(self, d: dict):
				self.__dict__.update(d)
		aset = _Obj(ai_settings)
		base_url, api_key = self._resolve_model_credentials(model)
		if user_id and prompt_key:
			messages = [{"role": "system", "content": system_prompt}]
			conv_id = await self.conv_repo.get_or_create(user_id=int(user_id), prompt_key=str(prompt_key), channel_id=int(getattr(aset, "channel_id", 0) or ai_settings.get("channel_id", 0)))
			budget = max(512, int((aset.max_tokens or 2000) * 0.7))
			summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
			if summary:
				messages.append({"role": "system", "content": f"Сводка контекста:\n{summary}"})
			recent = await self.conv_repo.list_recent_by_tokens(conv_id, max_tokens=budget - int(summary_tokens or 0))
			messages.extend(recent)
			messages.append({"role": "user", "content": user_prompt})
			await self.conv_repo.append(conv_id, role="user", content=user_prompt, tokens=0)
			result = await self._call_openrouter_messages(messages, model=model, temperature=aset.temperature, top_p=aset.top_p, max_tokens=aset.max_tokens, base_url=base_url, api_key=api_key)
			if result.get("success"):
				await self.conv_repo.append(conv_id, role="assistant", content=result.get("text") or "", tokens=0)
		else:
			result = await self._call_openrouter(system_prompt, user_prompt, model, aset.temperature, aset.top_p, aset.max_tokens, base_url=base_url, api_key=api_key)
		# учёт токенов при успехе
		if result.get("success"):
			cid = int(getattr(aset, "channel_id", 0) or ai_settings.get("channel_id", 0) or 0)
			if cid:
				await self.ai_repo.increment_tokens(cid, int(result.get("tokens_used", 0)))
		return result

	async def postprocess(self, text: str, *, ai_settings: dict) -> str:
		"""Единая пост-обработка (модерация, прочее)."""
		class _Obj:
			def __init__(self, d: dict):
				self.__dict__.update(d)
		aset = _Obj(ai_settings)
		if getattr(aset, "moderation_enabled", False):
			text = await self._moderate_text(text, aset)
		return text

	async def run_pipeline(self, *, channel_id: int, mode: str, topic: str = "", original_text: str = "", url: str = "", instruction: str | None = None, extra: dict | None = None, user_id: int | None = None, prompt_key: str | None = None) -> dict:
		"""Унифицированный пайплайн: build_prompt → generate_with_model → postprocess."""
		ai_settings, model, system_prompt, user_prompt = await self.build_prompt(channel_id, mode=mode, topic=topic, original_text=original_text, url=url, instruction=instruction, extra=extra)
		res = await self.generate_with_model(ai_settings=ai_settings, model=model, system_prompt=system_prompt, user_prompt=user_prompt, user_id=user_id, prompt_key=prompt_key)
		if res.get("success"):
			res["text"] = await self.postprocess(res.get("text") or "", ai_settings=ai_settings)
		return res

	async def _is_pro_channel(self, channel_id: int) -> bool:
		"""Определить план канала по владельцу (Client.is_premium)."""
		try:
			async with self.session.bind.connect() as _:
				pass
		except Exception:
			# session может быть уже валидной; продолжаем
			pass
		try:
			repo = ChannelsRepo(self.session)
			ch = await repo.get_by_id(channel_id)
			if not ch:
				return False
			owner = await self.session.get(Client, int(getattr(ch, "owner_id", 0)))
			return bool(getattr(owner, "is_premium", False)) if owner else False
		except Exception:
			return False
	
	async def _build_prompts(self, ai_settings, topic: str, extra_vars: dict) -> tuple[str, str]:
		"""Построить системный и пользовательский промпты с подстановкой переменных.
		Учитывает сценарии: обычная генерация ({topic}), улучшение текста ({original_text}),
		генерация из ссылки/статьи ({article_text}, {source_url})."""
		
		# Правила тона для подстановки
		tone_rules_map = {
			"friendly": "Пиши простыми словами, дружелюбно. Избегай канцелярита. Эмодзи 0–2 по делу.",
			"expert": "Точность важнее креатива. Без эмодзи. Терминологию поясняй коротко. Никаких голословных утверждений.",
			"conversational": "Легкий разговорный тон. Допусти риторические вопросы. Эмодзи до 3, не в каждом предложении.",
			"official": "Строго, нейтрально, без эмоциональных окрасов и эмодзи. Краткие, информативные формулировки.",
			"storytelling": "Начни с хука. Дай мини‑историю: проблема → поворот → вывод. Эмодзи максимум 1.",
			"promo": "Четко обозначь пользу. Ограничься фактами. Ясный CTA в конце. Без ложного дефицита.",
		}
		current_tone = getattr(ai_settings, "tone", "friendly")
		tone_rules_text = tone_rules_map.get(current_tone, tone_rules_map.get("friendly", ""))

		# Мягкие локальные преднастройки длины/эмодзи по тону (не сохраняем в БД)
		base_length = getattr(ai_settings, "length", "medium")
		base_emoji_level = int(getattr(ai_settings, "emoji_level", 1) or 0)
		if current_tone in ("expert", "official"):
			effective_length = base_length if base_length in ("short", "medium") else "short"
			effective_emoji_level = 0
		elif current_tone in ("promo",):
			effective_length = "short"
			effective_emoji_level = max(1, base_emoji_level)
		elif current_tone in ("friendly", "conversational", "storytelling"):
			effective_length = base_length if base_length in ("medium", "short") else "medium"
			effective_emoji_level = max(1, base_emoji_level)
		else:
			effective_length = base_length
			effective_emoji_level = base_emoji_level

		# Определим дефолтные шаблоны по типу задачи
		instruction = (extra_vars.get("instruction") or "").strip()
		has_original = bool(extra_vars.get("original_text"))
		has_article = bool(extra_vars.get("article_text"))
		if has_original:
			default_system = (
				f"Ты — редактор текстов для Telegram. Задача: {instruction} исходный текст. "
				"Сохраняй факты и смысл, убирай воду, делай структуру удобочитаемой. "
				"Соблюдай {tone}, {length}, {emoji_level}, язык {lang}. Ничего не выдумывай.\n"
				"Правила тона: {tone_rules}"
			)
			default_user = (
				"Исходный текст:\n{original_text}\n\n"
				"Контекст (опц.):\n"
				"Цель: {goal}\n"
				"Аудитория: {audience}\n"
				"Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}; Язык: {lang}\n\n"
				"Инструкция:\n{instruction}\n\n"
				"Перепиши с выходной структурой:\n"
				"1) Хук 1–2 строки.\n"
				"2) 3–6 коротких абзацев или список.\n"
				"3) Отдельной строкой CTA (если есть {cta}).\n"
				"4) Хэштеги (если есть {hashtags}).\n"
				"Ссылки из {links} интегрируй по месту краткими подписями."
			)
		elif has_article:
			default_system = (
				"Ты — копирайтер для Telegram. На основе статьи создай пост. "
				"Сохраняй факты, не придумывай. Стиль: {tone}, длина: {length}, "
				"эмодзи: {emoji_level}, язык: {lang}. Если информации мало — пиши нейтрально и безопасно.\n"
				"Правила тона: {tone_rules}"
			)
			default_user = (
				"Статья/текст источника:\n{article_text}\n\n"
				"Источник: {source_url}\n"
				"Тема: {topic}\n"
				"Цель: {goal}\n"
				"Аудитория: {audience}\n"
				"CTA: {cta}\n"
				"Хэштеги: {hashtags}\n"
				"Ссылки: {links}\n"
				"Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}; Язык: {lang}\n\n"
				"Сформируй пост со структурой:\n"
				"1) Хук 1–2 строки.\n"
				"2) 3–6 коротких абзацев или список (ключевые выводы из статьи).\n"
				"3) Отдельной строкой CTA (если задан).\n"
				"4) Хэштеги (если заданы)."
			)
		else:
			default_system = (
				"Ты — автор постов для Telegram‑каналов. Пиши ясно и по делу, без упоминания, что ты ИИ. "
				"Соблюдай заданные {tone}, {length}, {emoji_level}, язык {lang}. Не выдумывай факты.\n"
				"Правила тона: {tone_rules}"
			)
			default_user = (
				"Тема: {topic}\n"
				"Цель поста: {goal}\n"
				"Аудитория: {audience}\n"
				"Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}; Язык: {lang}\n"
				"CTA: {cta}\n"
				"Хэштеги: {hashtags}\n"
				"Ссылки: {links}\n\n"
				"Сформируй пост со структурой:\n"
				"1) Хук 1–2 строки.\n"
				"2) Основная часть: 3–6 коротких абзацев или список (1–2 предложения на пункт).\n"
				"3) Отдельной строкой CTA, если задан.\n"
				"4) В конце хэштеги, если заданы."
			)
		
		force_custom = bool(extra_vars.get("force_custom", False))
		
		# Приоритет: (force_custom и есть активный кастом) → активный кастом → пресет → обычный кастом → дефолт
		active_custom = await self.custom_repo.get_active(ai_settings.channel_id) if hasattr(ai_settings, "channel_id") else None
		if force_custom and (active_custom or ai_settings.custom_prompt):
			system_template = ai_settings.custom_prompt
			user_template = ai_settings.user_prompt_template or default_user
		elif active_custom:
			system_template = active_custom.content
			user_template = ai_settings.user_prompt_template or default_user
		elif ai_settings.preset_id:
			preset = await self.presets_repo.get_by_id(ai_settings.preset_id)
			if preset:
				system_template = preset.system_prompt
				user_template = preset.user_template
			else:
				system_template = default_system
				user_template = default_user
		elif ai_settings.custom_prompt:
			system_template = ai_settings.custom_prompt
			user_template = ai_settings.user_prompt_template or default_user
		else:
			system_template = default_system
			user_template = default_user
		
		# Подготавливаем переменные для подстановки
		tone_labels = {"friendly": "дружелюбный", "expert": "экспертный", "official": "официальный", "provocative": "провокационный"}
		length_labels = {"short": "короткий (до 500 символов)", "medium": "средний (500-1200 символов)", "long": "длинный (1200+ символов)"}
		
		variables = {
			"topic": topic,
			"tone": tone_labels.get(current_tone, current_tone),
			"length": length_labels.get(effective_length, effective_length),
			"emoji_level": str(effective_emoji_level),
			"lang": ai_settings.lang,
			"tone_rules": tone_rules_text,
			"hashtags": f"Добавь {ai_settings.hashtags_count} хештега" if ai_settings.hashtags_enabled else "Без хештегов",
			"cta": "Добавь призыв к действию в конце" if ai_settings.cta_enabled else "",
			"goal": extra_vars.get("goal", ""),
			"brand": extra_vars.get("brand", ""),
			"audience": extra_vars.get("audience", "подписчики канала"),
			"channel_title": extra_vars.get("channel_title", ""),
			"instruction": instruction,
			"original_text": extra_vars.get("original_text", ""),
			"article_text": extra_vars.get("article_text", ""),
			"source_url": extra_vars.get("source_url", ""),
			"links": extra_vars.get("links", ""),
			# Частые плейсхолдеры в пользовательских шаблонах — заполним пустым по умолчанию
			"schedule": extra_vars.get("schedule", ""),
			"date": extra_vars.get("date", ""),
			"time": extra_vars.get("time", ""),
			"city": extra_vars.get("city", ""),
		}
		
		# Безопасное форматирование: отсутствующие ключи заменяются на пустую строку
		class _SafeDict(dict):
			def __missing__(self, key):
				return ""
		def _safe_format(tpl: str, vars_: dict) -> str:
			try:
				return tpl.format_map(_SafeDict(vars_))
			except Exception:
				# В случае неожиданных ошибок вернём исходный шаблон
				return tpl
		
		system_prompt = _safe_format(system_template, variables)
		user_prompt = _safe_format(user_template, variables)
		
		return system_prompt, user_prompt

	def _build_openrouter_headers(self, api_key: str) -> dict:
		return {
			"Authorization": f"Bearer {api_key}",
			"Content-Type": "application/json",
			"HTTP-Referer": "https://github.com/your-bot",
		}

	def _build_openrouter_payload(self, *, system_prompt: str | None = None, user_prompt: str | None = None, messages: list[dict] | None = None, model: str, temperature: float, top_p: float, max_tokens: int) -> dict:
		if messages is None:
			messages = [
				{"role": "system", "content": system_prompt or ""},
				{"role": "user", "content": user_prompt or ""},
			]
		return {
			"model": model,
			"messages": messages,
			"temperature": temperature,
			"top_p": top_p,
			"max_tokens": max_tokens,
			"reasoning": {"enabled": False},
		}

	async def _post_openrouter(self, payload: dict, *, base_url: str, api_key: str) -> dict:
		url = f"{base_url}/chat/completions"
		headers = self._build_openrouter_headers(api_key)
		try:
			async with httpx.AsyncClient(timeout=60.0) as client:
				response = await client.post(url, headers=headers, json=payload)
				response.raise_for_status()
				data = response.json()
			if "choices" not in data or not data["choices"]:
				return {"success": False, "error": "Пустой ответ от API", "text": None, "tokens_used": 0}
			text = data["choices"][0]["message"]["content"].strip()
			tokens_used = data.get("usage", {}).get("total_tokens", 0)
			return {"success": True, "text": text, "tokens_used": tokens_used, "error": None}
		except httpx.HTTPStatusError as e:
			error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
			logger.error(f"❌ OpenRouter HTTP error: {error_msg}")
			return {"success": False, "error": error_msg, "text": None, "tokens_used": 0}
		except Exception as e:
			error_msg = f"Ошибка генерации: {str(e)}"
			logger.error(f"❌ OpenRouter error: {error_msg}")
			return {"success": False, "error": error_msg, "text": None, "tokens_used": 0}

	async def _call_openrouter(self, system_prompt: str, user_prompt: str, model: str,
								temperature: float, top_p: float, max_tokens: int, *, base_url: str | None = None, api_key: str | None = None) -> dict:
		"""Вызов OpenRouter API для генерации текста."""
		api_key = api_key or settings.openrouter_api_key
		base_url = base_url or settings.openrouter_base_url
		if not api_key:
			return {"success": False, "error": "OpenRouter API key не настроен", "text": None, "tokens_used": 0}
		payload = self._build_openrouter_payload(system_prompt=system_prompt, user_prompt=user_prompt, model=model, temperature=temperature, top_p=top_p, max_tokens=max_tokens)
		return await self._post_openrouter(payload, base_url=base_url, api_key=api_key)
		
	async def _moderate_text(self, text: str, ai_settings) -> str:
		"""Применить модерацию к сгенерированному тексту."""
		
		if ai_settings.forbidden_words:
			text_lower = text.lower()
			for word in ai_settings.forbidden_words:
				if word.lower() in text_lower:
					text = text.replace(word, "***")
					logger.warning(f"⚠️ Модерация: заменено запрещённое слово '{word}'")
		
		return text
		
	async def generate_from_link(self, channel_id: int, url: str, mode: str = "summary", *, force_custom: bool = False, user_id: int | None = None, prompt_key: str | None = None) -> dict:
		"""Генерация поста из ссылки (парсинг + саммари/рерайт)."""
		
		# Получаем настройки
		ai_settings = await self.ai_repo.get_or_create(channel_id)
		# Эффективные лимиты и кэп на запрос
		eff_day, eff_month, req_cap = await self._effective_limits(ai_settings, channel_id)
		# Дневной лимит в Pro отключён — используем только месячный
		if eff_month is not None and int(ai_settings.tokens_used_month or 0) >= int(eff_month):
			return {"success": False, "text": "", "tokens_used": 0, "error": "Превышен месячный лимит токенов"}
		if (ai_settings.max_tokens or 0) > int(req_cap):
			ai_settings.max_tokens = int(req_cap)
		# Лимит на Free: 3 саммари/сутки
		is_pro = await self._is_pro_channel(channel_id)
		if not is_pro:
			# используем простейший счётчик в памяти БД через conversations summary_tokens как хранилище
			try:
				# заведём специальный prompt_key для счётчика суточных саммари
				from datetime import datetime, timezone
				day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
				conv_id = await self.conv_repo.get_or_create(user_id=0, prompt_key=f"summary_day_{day_key}", channel_id=channel_id)
				# считаем количество записей assistant за сегодня как прокси (не идеально, но дёшево)
				recent = await self.conv_repo.list_recent_by_tokens(conv_id, max_tokens=10)
				count_today = sum(1 for m in recent if m.get("role") == "assistant")
				if count_today >= 3:
					return {"success": False, "text": "", "tokens_used": 0, "error": "Лимит саммари в Free: 3 в сутки. Доступно в Pro без ограничений."}
			except Exception:
				pass
		preset = None
		if ai_settings.preset_id:
			preset = await self.presets_repo.get_by_id(ai_settings.preset_id)
		
		# Парсим контент по URL
		try:
			async with httpx.AsyncClient(timeout=30.0) as client:
				response = await client.get(url, follow_redirects=True)
				response.raise_for_status()
				html_content = response.text
		except Exception as e:
			logger.error(f"Ошибка загрузки URL {url}: {e}")
			return {"success": False, "text": "", "tokens_used": 0, "error": f"Не удалось загрузить страницу: {str(e)}"}
		
		# Извлекаем текст из HTML
		try:
			soup = BeautifulSoup(html_content, 'lxml')
			for script in soup(["script", "style", "nav", "header", "footer"]):
				script.decompose()
			text = soup.get_text(separator='\n', strip=True)
			lines = [line.strip() for line in text.split('\n') if line.strip()]
			article_text = '\n'.join(lines)
			if len(article_text) > 8000:
				article_text = article_text[:8000] + "..."
		except Exception as e:
			logger.error(f"Ошибка парсинга HTML: {e}")
			return {"success": False, "text": "", "tokens_used": 0, "error": f"Не удалось извлечь текст: {str(e)}"}
		
		# Инструкция по режиму
		mode_instructions = {
			"summary": "Создай краткое саммари (резюме) этой статьи для Telegram-поста.",
			"rewrite": "Перепиши эту статью своими словами, сохраняя ключевые идеи.",
			"paraphrase": "Перефразируй эту статью, сделав её более понятной."
		}
		instruction = mode_instructions.get(mode, mode_instructions["summary"])
		
		# Собираем промпт через билдер с учётом пресета/кастома
		system_prompt, user_prompt = await self._build_prompts(
			ai_settings,
			topic="",
			extra_vars={"article_text": article_text, "source_url": url, "instruction": instruction, "force_custom": force_custom}
		)
		
		# Выбор модели с учётом режима
		chosen_model = self._pick_model(ai_settings, mode=mode)
		base_url, api_key = self._resolve_model_credentials(chosen_model)

		# Память диалога при наличии user_id/prompt_key
		if user_id and prompt_key:
			messages = [{"role": "system", "content": system_prompt}]
			conv_id = await self.conv_repo.get_or_create(user_id=int(user_id), prompt_key=str(prompt_key), channel_id=channel_id)
			budget = max(512, int((ai_settings.max_tokens or 2000) * 0.7))
			summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
			if summary:
				messages.append({"role": "system", "content": f"Сводка контекста:\n{summary}"})
			recent = await self.conv_repo.list_recent_by_tokens(conv_id, max_tokens=budget - int(summary_tokens or 0))
			messages.extend(recent)
			messages.append({"role": "user", "content": user_prompt})
			await self.conv_repo.append(conv_id, role="user", content=user_prompt, tokens=0)
			result = await self._call_openrouter_messages(
				messages,
				model=chosen_model,
				temperature=ai_settings.temperature,
				top_p=ai_settings.top_p,
				max_tokens=ai_settings.max_tokens,
				base_url=base_url,
				api_key=api_key,
			)
			if result.get("success"):
				await self.conv_repo.append(conv_id, role="assistant", content=result.get("text") or "", tokens=0)
		else:
			result = await self._call_openrouter(system_prompt, user_prompt, chosen_model, ai_settings.temperature, ai_settings.top_p, ai_settings.max_tokens, base_url=base_url, api_key=api_key)
		
		if result["success"]:
			await self.ai_repo.increment_tokens(channel_id, result.get("tokens_used", 0))
			# Для счётчика саммари на Free — отметим успешную генерацию
			if not is_pro:
				try:
					from datetime import datetime, timezone
					day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
					conv_id = await self.conv_repo.get_or_create(user_id=0, prompt_key=f"summary_day_{day_key}", channel_id=channel_id)
					await self.conv_repo.append(conv_id, role="assistant", content="ok", tokens=0)
				except Exception:
					pass
			if ai_settings.moderation_enabled:
				result["text"] = await self._moderate_text(result["text"], ai_settings)
		
		return result
		
	async def improve_text(self, channel_id: int, original_text: str, instruction: str = "улучши", *, force_custom: bool = False, user_id: int | None = None, prompt_key: str | None = None) -> dict:
		"""Улучшение существующего текста."""
		
		ai_settings = await self.ai_repo.get_or_create(channel_id)
		# Эффективные лимиты и кэп
		eff_day, eff_month, req_cap = await self._effective_limits(ai_settings, channel_id)
		if eff_day is not None and int(ai_settings.tokens_used_day or 0) >= int(eff_day):
			return {"success": False, "text": "", "tokens_used": 0, "error": "Превышен дневной лимит токенов"}
		if eff_month is not None and int(ai_settings.tokens_used_month or 0) >= int(eff_month):
			return {"success": False, "text": "", "tokens_used": 0, "error": "Превышен месячный лимит токенов"}
		if (ai_settings.max_tokens or 0) > int(req_cap):
			ai_settings.max_tokens = int(req_cap)

		# Собираем промпт через билдер с учётом пресета/кастома
		system_prompt, user_prompt = await self._build_prompts(
			ai_settings,
			topic="",
			extra_vars={"original_text": original_text, "instruction": instruction, "force_custom": force_custom}
		)

		# Выбор модели: по умолчанию считаем режим "rewrite" для улучшения текста
		chosen_model = self._pick_model(ai_settings, mode="rewrite")
		base_url, api_key = self._resolve_model_credentials(chosen_model)

		# Подготовка диалога (если передан user_id и prompt_key)
		messages = [
			{"role": "system", "content": system_prompt},
		]
		if user_id and prompt_key:
			conv_id = await self.conv_repo.get_or_create(user_id=user_id, prompt_key=prompt_key, channel_id=channel_id)
			# бюджет токенов на историю
			budget = max(512, int((ai_settings.max_tokens or 2000) * 0.7))
			summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
			if summary:
				messages.append({"role": "system", "content": f"Сводка контекста:\n{summary}"})
			recent = await self.conv_repo.list_recent_by_tokens(conv_id, max_tokens=budget - int(summary_tokens or 0))
			messages.extend(recent)
			# текущий вход
			messages.append({"role": "user", "content": user_prompt})
			# Запишем вход в историю заранее
			await self.conv_repo.append(conv_id, role="user", content=user_prompt, tokens=0)
			# Вызов модели
			result = await self._call_openrouter_messages(messages, model=chosen_model, temperature=ai_settings.temperature, top_p=ai_settings.top_p, max_tokens=ai_settings.max_tokens, base_url=base_url, api_key=api_key)
			if result.get("success"):
				answer = result.get("text") or ""
				await self.conv_repo.append(conv_id, role="assistant", content=answer, tokens=0)
		else:
			# Без памяти диалога — обычный вызов
			result = await self._call_openrouter(system_prompt, user_prompt, chosen_model, ai_settings.temperature, ai_settings.top_p, ai_settings.max_tokens, base_url=base_url, api_key=api_key)
		
		if result["success"]:
			await self.ai_repo.increment_tokens(channel_id, result.get("tokens_used", 0))
			if ai_settings.moderation_enabled:
				result["text"] = await self._moderate_text(result["text"], ai_settings)
		
		return result

	async def _call_openrouter_messages(self, messages: list[dict], *, model: str, temperature: float, top_p: float, max_tokens: int, base_url: str | None = None, api_key: str | None = None) -> dict:
		"""Вызов OpenRouter с произвольным списком сообщений (для памяти диалога)."""
		api_key = api_key or settings.openrouter_api_key
		base_url = base_url or settings.openrouter_base_url
		if not api_key:
			return {"success": False, "error": "OpenRouter API key не настроен", "text": None, "tokens_used": 0}
		payload = self._build_openrouter_payload(messages=messages, model=model, temperature=temperature, top_p=top_p, max_tokens=max_tokens)
		res = await self._post_openrouter(payload, base_url=base_url, api_key=api_key)
		if res.get("success"):
			logger.info(f"✅ OpenRouter(chat): {res.get('tokens_used', 0)} токенов")
		return res

	def _pick_model(self, ai_settings, mode: str) -> str:
		"""Вернуть модель с учётом пер-режимных overrides в filters['ai_models']."""
		try:
			filters = dict(getattr(ai_settings, "filters", {}) or {})
			ai_models = dict(filters.get("ai_models", {}) or {})
			return (ai_models.get(mode) or ai_models.get(mode.replace("-", "_")) or ai_settings.model)
		except Exception:
			return ai_settings.model

	def _resolve_model_credentials(self, model_code: str) -> tuple[str | None, str | None]:
		"""Вернуть (base_url, api_key) для конкретной модели из AI_MODELS_JSON; иначе дефолты settings."""
		raw = (settings.ai_models_json or "").strip()
		if not raw:
			return settings.openrouter_base_url, settings.openrouter_api_key
		try:
			data = json.loads(raw)
			if isinstance(data, list):
				for it in data:
					if isinstance(it, dict) and str(it.get("code")) == model_code:
						return (it.get("base_url") or settings.openrouter_base_url, it.get("api_key") or settings.openrouter_api_key)
			elif isinstance(data, dict):
				# {code: label}
				return settings.openrouter_base_url, settings.openrouter_api_key
		except Exception:
			pass
		return settings.openrouter_base_url, settings.openrouter_api_key

	async def _effective_limits(self, ai_settings, channel_id: int) -> tuple[int | None, int | None, int]:
		"""Вернуть (дневной лимит, месячный лимит, кап на запрос) с учётом плана и настроек канала."""
		is_pro = await self._is_pro_channel(channel_id)
		# Плановые значения по умолчанию
		plan_day = None if is_pro else 5_000
		plan_month = 2_000_000 if is_pro else 50_000
		# Если в БД явно задан лимит — используем его, иначе плановый
		eff_day = int(ai_settings.tokens_limit_day) if getattr(ai_settings, "tokens_limit_day", None) else plan_day
		eff_month = int(ai_settings.tokens_limit_month) if getattr(ai_settings, "tokens_limit_month", None) else plan_month
		# Кэп на запрос
		req_cap = 4096 if is_pro else 1024
		return eff_day, eff_month, req_cap
