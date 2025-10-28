from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message, InlineKeyboardMarkup, InlineKeyboardButton
from contextlib import suppress
from loguru import logger
from sqlalchemy import select
from datetime import datetime, timezone, timedelta
import asyncio

from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.external_bots import ChannelBotsRepo
from app.repositories.subscribers import SubscribersRepo
from app.domain.models import JoinRequest


def build_shared_join_router(external_bot_id: int) -> Router:
	router = Router()

	@router.chat_join_request()
	async def on_join_request(event: ChatJoinRequest):
		try:
			from app.services.join_requests import JoinRequestsService
			async with AsyncSessionLocal() as session:
				service = JoinRequestsService(event.bot, session)
				await service.handle_join_request(external_bot_id, event)
		except Exception as e:
			logger.exception(f"join request error: {e}")

	@router.chat_member()
	async def on_chat_member(update: ChatMemberUpdated):
		try:
			chat_id = int(getattr(update.chat, "id", 0))
			new = getattr(update, "new_chat_member", None)
			old = getattr(update, "old_chat_member", None)
			if not chat_id or not new or not old:
				return
			if str(getattr(old, "status", "")) in ("member", "administrator") and str(getattr(new, "status", "")) == "left":
				user = getattr(update, "from_user", None) or getattr(new, "user", None)
				user_id = int(getattr(user, "id", 0)) if user else 0
				if not user_id:
					return
				async with AsyncSessionLocal() as session:
					ch_repo = ChannelsRepo(session)
					cb_repo = ChannelBotsRepo(session)
					ch = await ch_repo.get_by_chat_id(chat_id)
					if not ch:
						return
					cb = await cb_repo.get_by_channel_id(ch.id)
					if not cb or int(cb.external_bot_id) != int(external_bot_id):
						return
					if cb.farewell_text:
						with suppress(Exception):
							await update.bot.send_message(user_id, cb.farewell_text)
		except Exception:
			pass

	@router.callback_query()
	async def on_dm_solve(callback):
		try:
			data = getattr(callback, "data", "")
			if not (isinstance(data, str) and data.startswith("dm_solve:")):
				return
			cid = int(data.split(":")[-1])
			user = getattr(callback, "from_user", None)
			user_id = int(getattr(user, "id", 0)) if user else 0
			if not (cid and user_id):
				return
			async with AsyncSessionLocal() as session:
				ch_repo = ChannelsRepo(session)
				cb_repo = ChannelBotsRepo(session)
				subs_repo = SubscribersRepo(session)
				ch = await ch_repo.get_by_id(cid)
				if not ch:
					return
				cb = await cb_repo.get_by_channel_id(cid)
				if not cb or int(cb.external_bot_id) != int(external_bot_id):
					return
				# авто-режим: сразу approve
				if int(getattr(cb, "mode", 0)) == 0:
					with suppress(Exception):
						await callback.bot.approve_chat_join_request(chat_id=int(ch.tg_chat_id), user_id=user_id)
					# upsert подписчика
					from app.services.subscribers import save_subscriber_preference
					with suppress(Exception):
						await save_subscriber_preference(session, channel_id=cid, user_id=user_id, username=getattr(user, "username", None), full_name=getattr(user, "full_name", None))
				with suppress(Exception):
					await callback.answer("Ваша заявка принята", show_alert=False)
				with suppress(Exception):
					await callback.message.edit_text("Спасибо! Проверка пройдена.")
		except Exception:
			pass

	@router.message(F.text)
	async def on_dm_text(message: Message):
		try:
			user = getattr(message, "from_user", None)
			user_id = int(getattr(user, "id", 0)) if user else 0
			if not user_id:
				return
			text = (message.text or "").strip()
			async with AsyncSessionLocal() as session:
				# найдём все актуальные pending-заявки пользователя
				res = await session.execute(
					select(JoinRequest).where(
						JoinRequest.user_id == user_id,
						JoinRequest.status == "pending",
						(JoinRequest.expires_at.is_(None)) | (JoinRequest.expires_at > datetime.now(timezone.utc)),
					)
				)
				items = list(res.scalars().all())
				if not items:
					return
				# обрабатываем по одному (если несколько каналов — принимает первый совпавший)
				for jr in items:
					ctype = (jr.challenge_type or "").lower()
					ok = False
					if ctype == "captcha":
						pp = dict(jr.challenge_payload or {})
						expected = str(pp.get("answer", "")).strip()
						ok = (text == expected)
					elif ctype == "keyword":
						# достанем конфиг канала
						cb_repo = ChannelBotsRepo(session)
						cfg = await cb_repo.get_require_dm_config(jr.channel_id)
						word = str(cfg.get("keyword_word", "START"))
						ok = (text.strip().lower() == word.strip().lower())
					elif ctype == "simple":
						# текст сообщения для simple не обязателен; кнопка уже покрывает кейс, но примем любое сообщение как подтверждение
						ok = True
					# учёт попыток
					if not ok:
						try:
							cur = int(jr.attempts_left or 1)
							jr.attempts_left = max(0, cur - 1)
							await session.commit()
						except Exception:
							pass
						continue
					# успех: для авто-режима — апрув сразу
					cb_repo = ChannelBotsRepo(session)
					cb = await cb_repo.get_by_channel_id(jr.channel_id)
					if cb and int(getattr(cb, "mode", 0)) == 0:
						# approve
						ch_repo = ChannelsRepo(session)
						ch = await ch_repo.get_by_id(jr.channel_id)
						if ch:
							with suppress(Exception):
								await message.bot.approve_chat_join_request(chat_id=int(ch.tg_chat_id), user_id=user_id)
							from app.repositories.subscribers import SubscribersRepo as _Subs
							subs = _Subs(session)
							from app.services.subscribers import save_subscriber_preference
							with suppress(Exception):
								await save_subscriber_preference(session, channel_id=jr.channel_id, user_id=user_id, username=getattr(user, "username", None), full_name=getattr(user, "full_name", None))
							# добавим тег: сначала из заявки, иначе из simple_tag
							try:
								pp = dict(jr.challenge_payload or {})
								utm = pp.get("utm")
								if not utm:
									cfg = await cb_repo.get_require_dm_config(jr.channel_id)
									utm = (cfg.get("simple_tag") or "").strip()
								if utm:
									from app.services.subscribers import save_subscriber_preference
									with suppress(Exception):
										await save_subscriber_preference(session, channel_id=jr.channel_id, user_id=user_id, username=getattr(user, "username", None), full_name=getattr(user, "full_name", None), tag=utm)
							except Exception:
								pass
							# пометить как approved
							jr.status = "approved"
							await session.commit()
						# Показать краткое системное уведомление (имитация тоста) и удалить его
						with suppress(Exception):
							_tmp = await message.answer("Ваша заявка принята")
							async def _delete_later():
								await asyncio.sleep(3)
								with suppress(Exception):
									await message.bot.delete_message(user_id, _tmp.message_id)
						asyncio.create_task(_delete_later())
					else:
						# не авто — отметим challenge как решённый, оставим pending (готов к принятию)
						jr.attempts_left = 0
						# если utm отсутствует в заявке — сохраним simple_tag в payload сейчас
						try:
							pp = dict(jr.challenge_payload or {})
							if "utm" not in pp:
								cfg = await cb_repo.get_require_dm_config(jr.channel_id)
								utm = (cfg.get("simple_tag") or "").strip()
								if utm:
									pp["utm"] = utm
									jr.challenge_payload = pp
						except Exception:
							pass
						await session.commit()
					with suppress(Exception):
						await message.answer("Спасибо! Ожидайте одобрения модератора.")
					break
		except Exception:
			pass

	@router.message(CommandStart())
	async def on_start_deeplink(message: Message):
		try:
			text = message.text or ""
			parts = text.split(maxsplit=1)
			payload = parts[1] if len(parts) > 1 else ""
			if not (isinstance(payload, str) and payload.startswith("join_c_")):
				return
			# payload: join_c_{cid}[_utm]
			raw = payload.removeprefix("join_c_")
			utm = None
			try:
				cid_str, utm = raw.split("_", 1)
			except ValueError:
				cid_str = raw
			cid = int(cid_str)
			user = getattr(message, "from_user", None)
			user_id = int(getattr(user, "id", 0)) if user else 0
			if not (cid and user_id):
				return
			async with AsyncSessionLocal() as session:
				ch_repo = ChannelsRepo(session)
				cb_repo = ChannelBotsRepo(session)
				ch = await ch_repo.get_by_id(cid)
				if not ch:
					return
				cb = await cb_repo.get_by_channel_id(cid)
				if not cb or int(cb.external_bot_id) != int(external_bot_id):
					return
				# создадим pending, если нет
				from sqlalchemy import select as _select
				res = await session.execute(_select(JoinRequest).where(JoinRequest.channel_id == cid, JoinRequest.user_id == user_id, JoinRequest.status == "pending"))
				jr = res.scalars().first()
				if not jr:
					meta = dict(getattr(cb, "meta", {}) or {})
					require_mode = int(meta.get("require_dm_mode", 1))
					pp = {}
					if require_mode == 2:
						import random
						a = random.randint(2, 9)
						b = random.randint(2, 9)
						pp = {"a": a, "b": b, "answer": str(a + b)}
					from datetime import datetime, timezone, timedelta
					jr = JoinRequest(
						channel_id=cid,
						user_id=user_id,
						status="pending",
						challenge_type=("captcha" if require_mode == 2 else ("keyword" if require_mode == 3 else "simple")),
						challenge_payload=pp,
						expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)),
						attempts_left=3,
					)
					session.add(jr)
					await session.commit()
				# Сохраним utm в payload заявки
				try:
					jr_payload = dict(getattr(jr, "challenge_payload", {}) or {})
					if utm:
						jr_payload["utm"] = utm
						jr.challenge_payload = jr_payload
						await session.commit()
				except Exception:
					pass
				# Немедленно добавим (или обновим) подписчика и тег utm, чтобы он попал в экспорт CSV
				from app.repositories.subscribers import SubscribersRepo as _Subs
				subs = _Subs(session)
				from app.services.subscribers import save_subscriber_preference
				with suppress(Exception):
					await save_subscriber_preference(session, channel_id=cid, user_id=user_id, username=getattr(user, "username", None), full_name=getattr(user, "full_name", None), tag=utm)
				# отправим челендж по текущему пресету (и при simple пометим как решённый)
				cfg = await cb_repo.get_require_dm_config(cid)
				require_mode = await cb_repo.get_require_dm_mode(cid)
				if require_mode == 1:
					# simple: отмечаем challenge как решённый
					try:
						jr.attempts_left = 0
						await session.commit()
					except Exception:
						pass
					# auto-approve если авто-режим
					if int(getattr(cb, "mode", 0)) == 0:
						ch = await ch_repo.get_by_id(cid)
						if ch:
							with suppress(Exception):
								await message.bot.approve_chat_join_request(chat_id=int(ch.tg_chat_id), user_id=user_id)
							from app.repositories.subscribers import SubscribersRepo as _Subs
							subs = _Subs(session)
							with suppress(Exception):
								await subs.add(cid, user_id, getattr(user, "username", None), getattr(user, "full_name", None))
							# добавим тег сразу, если есть utm
							if utm:
								with suppress(Exception):
									await subs.add_tag(cid, user_id, utm)
							jr.status = "approved"
							await session.commit()
							# Показать краткое системное уведомление (имитация тоста) и удалить его
							with suppress(Exception):
								_tmp = await message.answer("Ваша заявка принята")
								async def _delete_later3():
									await asyncio.sleep(3)
									with suppress(Exception):
										await message.bot.delete_message(user_id, _tmp.message_id)
							asyncio.create_task(_delete_later3())
					else:
						with suppress(Exception):
							await message.answer("Спасибо! Ожидайте одобрения модератора.")
					with suppress(Exception):
						await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="OK", callback_data=f"dm_solve:{cid}")]]))
				elif require_mode == 2:
					res2 = await session.execute(_select(JoinRequest).where(JoinRequest.channel_id == cid, JoinRequest.user_id == user_id, JoinRequest.status == "pending"))
					jr2 = res2.scalars().first()
					pp = dict(getattr(jr2, "challenge_payload", {}) or {})
					a = pp.get("a")
					b = pp.get("b")
					msg = (cfg.get("captcha_prompt") or "Решите пример: {a} + {b} = ?").format(a=a, b=b)
					with suppress(Exception):
						await message.answer(msg)
				elif require_mode == 3:
					word = (cfg.get("keyword_word") or "START")
					msg = (cfg.get("keyword_prompt") or "Отправьте слово: {w}").format(w=word)
					with suppress(Exception):
						await message.answer(msg)
		except Exception:
			pass

	return router


