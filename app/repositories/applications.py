from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import Application
from app.repositories.base import BaseRepository


class ApplicationsRepo(BaseRepository[Application]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Application)

    async def get_mode(self, channel_id: int) -> int:
        res = await self.session.execute(select(Application).where(Application.channel_id == channel_id))
        obj = res.scalars().first()
        return int(getattr(obj, "mode", 0)) if obj else 0

    async def set_mode(self, channel_id: int, mode: int) -> None:
        res = await self.session.execute(select(Application).where(Application.channel_id == channel_id))
        obj = res.scalars().first()
        if not obj:
            obj = Application(channel_id=channel_id, user_id=0, mode=mode)
            self.session.add(obj)
        else:
            obj.mode = mode
        await self.session.commit()


