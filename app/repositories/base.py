from __future__ import annotations

from typing import Generic, TypeVar, Type, Sequence, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func


T = TypeVar("T")


class BaseRepository(Generic[T]):
	"""Базовый репозиторий: CRUD, пагинация, простейший upsert по уникальному ключу.

	Ожидает, что у модели есть autoincrement PK id и что переданный model_cls — ORM-класс.
	"""

	def __init__(self, session: AsyncSession, model_cls: Type[T]):
		self.session = session
		self.model_cls = model_cls

	async def get_by_id(self, obj_id: Any) -> T | None:
		return await self.session.get(self.model_cls, obj_id)

	async def list(self, *, offset: int = 0, limit: int = 50) -> list[T]:
		res = await self.session.execute(select(self.model_cls).offset(offset).limit(limit))
		return list(res.scalars().all())

	async def count(self) -> int:
		res = await self.session.execute(select(func.count()).select_from(self.model_cls))
		return int(res.scalar_one())

	async def create(self, **fields) -> T:
		obj = self.model_cls(**fields)
		self.session.add(obj)
		await self.session.commit()
		await self.session.refresh(obj)
		return obj

	async def update(self, obj_id: Any, **fields) -> bool:
		obj = await self.session.get(self.model_cls, obj_id)
		if not obj:
			return False
		for k, v in fields.items():
			setattr(obj, k, v)
		await self.session.commit()
		return True

	async def delete(self, obj_id: Any) -> bool:
		obj = await self.session.get(self.model_cls, obj_id)
		if not obj:
			return False
		await self.session.delete(obj)
		await self.session.commit()
		return True

	async def upsert_unique(self, *, where: dict, values: dict) -> T:
		"""Простейший upsert: ищет по where; если не найден — создаёт, иначе обновляет поля values."""
		stmt = select(self.model_cls)
		for k, v in where.items():
			stmt = stmt.where(getattr(self.model_cls, k) == v)
		res = await self.session.execute(stmt)
		obj = res.scalars().first()
		if obj is None:
			obj = self.model_cls(**{**where, **values})
			self.session.add(obj)
			await self.session.commit()
			await self.session.refresh(obj)
			return obj
		for k, v in values.items():
			setattr(obj, k, v)
		await self.session.commit()
		return obj



