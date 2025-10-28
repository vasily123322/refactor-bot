from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import AdminConfig, BannedChat


class AdminConfigRepo:
	def __init__(self, session: AsyncSession):
		self.session = session

	async def get(self) -> AdminConfig | None:
		res = await self.session.execute(select(AdminConfig).order_by(AdminConfig.id.asc()).limit(1))
		return res.scalars().first()

	async def get_log_chat_id(self) -> int | None:
		cfg = await self.get()
		return int(getattr(cfg, "log_chat_id", 0) or 0) or None

	async def set_log_chat(self, chat_id: int) -> AdminConfig:
		cfg = await self.get()
		if not cfg:
			cfg = AdminConfig(log_chat_id=chat_id)
			self.session.add(cfg)
		else:
			cfg.log_chat_id = chat_id
		await self.session.commit()
		await self.session.refresh(cfg)
		return cfg


class BansRepo:
	def __init__(self, session: AsyncSession):
		self.session = session

	async def is_banned(self, tg_chat_id: int) -> bool:
		res = await self.session.execute(select(BannedChat).where(BannedChat.tg_chat_id == tg_chat_id))
		return res.scalars().first() is not None

	async def ban(self, tg_chat_id: int, reason: str | None, created_by: int | None) -> BannedChat:
		res = await self.session.execute(select(BannedChat).where(BannedChat.tg_chat_id == tg_chat_id))
		obj = res.scalars().first()
		if obj:
			obj.reason = reason
			obj.created_by = created_by
			await self.session.commit()
			await self.session.refresh(obj)
			return obj
		obj = BannedChat(tg_chat_id=tg_chat_id, reason=reason, created_by=created_by)
		self.session.add(obj)
		await self.session.commit()
		await self.session.refresh(obj)
		return obj

	async def unban(self, tg_chat_id: int) -> bool:
		await self.session.execute(delete(BannedChat).where(BannedChat.tg_chat_id == tg_chat_id))
		await self.session.commit()
		return True

	async def list_bans(self, limit: int = 50) -> list[BannedChat]:
		res = await self.session.execute(select(BannedChat).order_by(BannedChat.created_at.desc()).limit(limit))
		return list(res.scalars().all())








