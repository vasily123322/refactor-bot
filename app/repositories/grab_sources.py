from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import GrabSource


class GrabSourcesRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_for_target(self, channel_id: int) -> list[GrabSource]:
        res = await self.session.execute(
            select(GrabSource).where(GrabSource.target_channel_id == channel_id)
        )
        return list(res.scalars().all())

    async def add(self, source_chat_id: int, target_channel_id: int) -> GrabSource:
        obj = GrabSource(
            source_chat_id=source_chat_id, target_channel_id=target_channel_id
        )
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj

    async def get_by_id(self, gid: int) -> GrabSource | None:
        return await self.session.get(GrabSource, gid)

    async def delete_by_id(self, gid: int) -> None:
        obj = await self.session.get(GrabSource, gid)
        if not obj:
            return
        await self.session.delete(obj)
        await self.session.commit()
