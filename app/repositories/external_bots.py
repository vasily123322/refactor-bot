from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import ExternalBot, ChannelBot


class ExternalBotsRepo:
	def __init__(self, session: AsyncSession):
		self.session = session

	async def create_or_update(self, token: str, owner_client_id: int | None, bot_user_id: int | None, bot_username: str | None) -> ExternalBot:
		obj = None
		if bot_user_id:
			res = await self.session.execute(select(ExternalBot).where(ExternalBot.bot_user_id == bot_user_id))
			obj = res.scalars().first()
		if not obj:
			obj = ExternalBot(token=token, owner_client_id=owner_client_id, bot_user_id=bot_user_id, bot_username=bot_username, is_active=True)
			self.session.add(obj)
		else:
			obj.token = token
			obj.owner_client_id = owner_client_id
			obj.bot_username = bot_username
			obj.is_active = True
		await self.session.commit()
		await self.session.refresh(obj)
		return obj

	async def get_active(self) -> list[ExternalBot]:
		res = await self.session.execute(select(ExternalBot).where(ExternalBot.is_active == True))
		return list(res.scalars().all())

	async def get_by_id(self, external_bot_id: int) -> ExternalBot | None:
		return await self.session.get(ExternalBot, external_bot_id)

	async def deactivate_if_orphan(self, external_bot_id: int) -> None:
		res = await self.session.execute(select(func.count()).select_from(ChannelBot).where(ChannelBot.external_bot_id == external_bot_id))
		if int(res.scalar_one()) == 0:
			obj = await self.get_by_id(external_bot_id)
			if obj:
				obj.is_active = False
				await self.session.commit()


class ChannelBotsRepo:
	def __init__(self, session: AsyncSession):
		self.session = session

	async def get_by_channel_id(self, channel_id: int) -> ChannelBot | None:
		res = await self.session.execute(select(ChannelBot).where(ChannelBot.channel_id == channel_id))
		return res.scalars().first()

	async def bind(self, channel_id: int, external_bot_id: int) -> ChannelBot:
		obj = await self.get_by_channel_id(channel_id)
		if obj:
			obj.external_bot_id = external_bot_id
		else:
			obj = ChannelBot(channel_id=channel_id, external_bot_id=external_bot_id, mode=0)
			self.session.add(obj)
		await self.session.commit()
		await self.session.refresh(obj)
		return obj

	async def set_mode(self, channel_id: int, mode: int) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		obj.mode = mode
		await self.session.commit()

	async def get_require_dm(self, channel_id: int) -> bool:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return False
		meta = dict(getattr(obj, "meta", {}) or {})
		return bool(meta.get("require_dm", False))

	async def set_require_dm(self, channel_id: int, value: bool) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["require_dm"] = bool(value)
		obj.meta = meta
		await self.session.commit()

	async def get_require_dm_mode(self, channel_id: int) -> int:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return 1  # simple по умолчанию
		meta = dict(getattr(obj, "meta", {}) or {})
		return int(meta.get("require_dm_mode", 1))

	async def set_require_dm_mode(self, channel_id: int, mode: int) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["require_dm_mode"] = int(mode)
		obj.meta = meta
		await self.session.commit()

	async def get_require_dm_config(self, channel_id: int) -> dict:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return {}
		meta = dict(getattr(obj, "meta", {}) or {})
		return dict(meta.get("require_dm_config", {}) or {})

	async def set_require_dm_config(self, channel_id: int, config: dict) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["require_dm_config"] = dict(config or {})
		obj.meta = meta
		await self.session.commit()

	# --- Anti-raid ---
	async def get_anti_raid_enabled(self, channel_id: int) -> bool:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return False
		meta = dict(getattr(obj, "meta", {}) or {})
		return bool(meta.get("anti_raid_enabled", False))

	async def set_anti_raid_enabled(self, channel_id: int, value: bool) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["anti_raid_enabled"] = bool(value)
		obj.meta = meta
		await self.session.commit()

	async def get_anti_raid_threshold(self, channel_id: int) -> int:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return 30
		meta = dict(getattr(obj, "meta", {}) or {})
		try:
			return int(meta.get("anti_raid_threshold", 30))
		except Exception:
			return 30

	async def set_anti_raid_threshold(self, channel_id: int, threshold: int) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["anti_raid_threshold"] = int(max(1, threshold))
		obj.meta = meta
		await self.session.commit()

	# --- Reject templates ---
	async def get_reject_default(self, channel_id: int) -> str | None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return None
		meta = dict(getattr(obj, "meta", {}) or {})
		val = meta.get("reject_default")
		return str(val) if val is not None else None

	async def set_reject_default(self, channel_id: int, text: str | None) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["reject_default"] = (text or None)
		obj.meta = meta
		await self.session.commit()

	async def list_reject_templates(self, channel_id: int) -> list[str]:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return []
		meta = dict(getattr(obj, "meta", {}) or {})
		tpls = list(meta.get("reject_templates", []) or [])
		# нормализуем в строки
		return [str(x) for x in tpls if isinstance(x, (str, bytes)) and str(x).strip()]

	async def add_reject_template(self, channel_id: int, text: str) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		tpls = list(meta.get("reject_templates", []) or [])
		tpls.append(text)
		meta["reject_templates"] = tpls[-20:]  # ограничим до 20
		obj.meta = meta
		await self.session.commit()

	async def clear_reject_templates(self, channel_id: int) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["reject_templates"] = []
		obj.meta = meta
		await self.session.commit()

	# --- Request filters (whitelist/blacklist/stop-words) ---
	async def get_request_filters(self, channel_id: int) -> dict:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return {}
		meta = dict(getattr(obj, "meta", {}) or {})
		flt = dict(meta.get("request_filters", {}) or {})
		# normalize lists
		for key in ("whitelist_usernames", "blacklist_usernames", "stop_words"):
			vals = flt.get(key) or []
			flt[key] = [str(x).strip().lower() for x in vals if str(x).strip()]
		for key in ("whitelist_ids", "blacklist_ids"):
			vals = flt.get(key) or []
			try:
				flt[key] = [int(x) for x in vals if str(x).strip()]
			except Exception:
				flt[key] = []
		return flt

	async def set_request_filters(self, channel_id: int, filters: dict) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		meta = dict(getattr(obj, "meta", {}) or {})
		meta["request_filters"] = filters or {}
		obj.meta = meta
		await self.session.commit()

	async def update_welcome(self, channel_id: int, text: str | None) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		obj.welcome_text = text
		await self.session.commit()

	async def update_farewell(self, channel_id: int, text: str | None) -> None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return
		obj.farewell_text = text
		await self.session.commit()

	async def unbind(self, channel_id: int) -> int | None:
		obj = await self.get_by_channel_id(channel_id)
		if not obj:
			return None
		ext_id = obj.external_bot_id
		await self.session.delete(obj)
		await self.session.commit()
		return ext_id



