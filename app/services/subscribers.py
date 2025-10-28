from __future__ import annotations

from contextlib import suppress
from sqlalchemy.ext.asyncio import AsyncSession


async def save_subscriber_preference(session: AsyncSession, *, channel_id: int, user_id: int, username: str | None, full_name: str | None, tag: str | None = None) -> bool:
	"""Идемпотентное добавление подписчика и (опционально) одного тега.

	Возвращает True при успешной записи (без гарантии о фактическом изменении).
	"""
	from app.repositories.subscribers import SubscribersRepo
	repo = SubscribersRepo(session)
	with suppress(Exception):
		await repo.add(channel_id, user_id, username, full_name)
	if tag:
		with suppress(Exception):
			await repo.add_tag(channel_id, user_id, str(tag))
	return True



