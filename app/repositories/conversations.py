from __future__ import annotations
from typing import Optional, Iterable
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import AIConversation, AIConversationMessage


class ConversationsRepo:
	def __init__(self, session: AsyncSession):
		self.session = session

	async def get_or_create(self, user_id: int, prompt_key: str, channel_id: int | None = None) -> int:
		stmt = select(AIConversation).where(AIConversation.user_id == user_id, AIConversation.prompt_key == prompt_key)
		res = await self.session.execute(stmt)
		conv = res.scalar_one_or_none()
		if conv:
			return int(conv.id)
		conv = AIConversation(user_id=user_id, prompt_key=prompt_key, channel_id=channel_id)
		self.session.add(conv)
		await self.session.flush()
		return int(conv.id)

	async def list_recent_by_tokens(self, conversation_id: int, max_tokens: int) -> list[dict]:
		stmt = (
			select(AIConversationMessage)
			.where(AIConversationMessage.conversation_id == conversation_id)
			.order_by(AIConversationMessage.created_at.desc())
		)
		res = await self.session.execute(stmt)
		rows = list(res.scalars())
		# накапливаем с конца до достижения лимита токенов
		total = 0
		selected: list[AIConversationMessage] = []
		for m in rows:
			if total + int(m.tokens or 0) > max_tokens:
				break
			selected.append(m)
			total += int(m.tokens or 0)
		selected = list(reversed(selected))
		return [{"role": m.role, "content": m.content} for m in selected]

	async def append(self, conversation_id: int, role: str, content: str, tokens: int = 0, meta: dict | None = None) -> None:
		msg = AIConversationMessage(
			conversation_id=conversation_id,
			role=role,
			content=content,
			tokens=int(tokens or 0),
			meta=meta or None,
		)
		self.session.add(msg)
		await self.session.flush()

	async def get_summary(self, conversation_id: int) -> tuple[Optional[str], int]:
		stmt = select(AIConversation).where(AIConversation.id == conversation_id)
		res = await self.session.execute(stmt)
		conv = res.scalar_one_or_none()
		if not conv:
			return None, 0
		return conv.last_summary, int(conv.summary_tokens or 0)

	async def upsert_summary(self, conversation_id: int, content: str, tokens: int) -> None:
		stmt = select(AIConversation).where(AIConversation.id == conversation_id)
		res = await self.session.execute(stmt)
		conv = res.scalar_one_or_none()
		if not conv:
			return
		conv.last_summary = content
		conv.summary_tokens = int(tokens or 0)
		await self.session.flush()

	async def delete_all_by_user(self, user_id: int) -> int:
		"""Удалить все диалоги пользователя (и их сообщения). Возвращает число удалённых диалогов."""
		res = await self.session.execute(select(AIConversation).where(AIConversation.user_id == user_id))
		rows = list(res.scalars().all())
		count = len(rows)
		for row in rows:
			await self.session.delete(row)
		await self.session.commit()
		return count

	async def delete_by_user_and_prompt_key(self, user_id: int, prompt_key: str) -> int:
		"""Удалить диалог по пользователю и prompt_key. Возвращает 1, если удалён."""
		res = await self.session.execute(select(AIConversation).where(AIConversation.user_id == user_id, AIConversation.prompt_key == prompt_key))
		row = res.scalar_one_or_none()
		if not row:
			return 0
		await self.session.delete(row)
		await self.session.commit()
		return 1

	async def delete_all_by_channel(self, channel_id: int) -> int:
		"""Удалить все диалоги, привязанные к каналу."""
		res = await self.session.execute(select(AIConversation).where(AIConversation.channel_id == channel_id))
		rows = list(res.scalars().all())
		count = len(rows)
		for row in rows:
			await self.session.delete(row)
		await self.session.commit()
		return count


