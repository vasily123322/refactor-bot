from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import PostTask


class PostsRepo:
	def __init__(self, session: AsyncSession):
		self.session = session

	async def get_by_dedupe(self, key: str) -> PostTask | None:
		res = await self.session.execute(select(PostTask).where(PostTask.dedupe_key == key))
		return res.scalars().first()

	async def get_by_id(self, pid: int) -> PostTask | None:
		return await self.session.get(PostTask, pid)


