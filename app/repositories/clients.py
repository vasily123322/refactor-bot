from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import Client
from app.repositories.base import BaseRepository


class ClientsRepo(BaseRepository[Client]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Client)

    async def create_or_get(self, tg_user_id: int, username: str | None, full_name: str | None) -> Client:
        res = await self.session.execute(select(Client).where(Client.tg_user_id == tg_user_id))
        obj = res.scalars().first()
        if obj:
            # Обновим базовые поля, если что-то изменилось
            changed = False
            if username is not None and obj.username != username:
                obj.username = username
                changed = True
            if full_name is not None and obj.full_name != full_name:
                obj.full_name = full_name
                changed = True
            if changed:
                await self.session.commit()
            return obj
        obj = Client(tg_user_id=tg_user_id, username=username, full_name=full_name)
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj


