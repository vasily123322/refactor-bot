from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import Client
from app.domain.ui_settings import merge_ui_settings
from app.repositories.base import BaseRepository


class ClientsRepo(BaseRepository[Client]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Client)

    async def create_or_get(
        self, tg_user_id: int, username: str | None, full_name: str | None
    ) -> Client:
        res = await self.session.execute(
            select(Client).where(Client.tg_user_id == tg_user_id)
        )
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

    async def get_ui_settings(self, client_id: int) -> dict[str, bool]:
        obj = await self.session.get(Client, client_id)
        if not obj:
            return merge_ui_settings(None)
        try:
            return merge_ui_settings(dict(obj.ui_settings or {}))
        except Exception:
            return merge_ui_settings(None)

    async def update_ui_setting(
        self, client_id: int, key: str, value: bool
    ) -> dict[str, bool]:
        obj = await self.session.get(Client, client_id)
        if not obj:
            raise ValueError("client not found")
        data = dict(obj.ui_settings or {})
        data[key] = bool(value)
        obj.ui_settings = data
        await self.session.commit()
        return merge_ui_settings(data)

    async def get_last_channel_id(self, client_id: int) -> int | None:
        obj = await self.session.get(Client, client_id)
        if not obj:
            return None
        val = getattr(obj, "last_channel_id", None)
        return int(val) if isinstance(val, int) else None

    async def update_last_channel_id(
        self, client_id: int, channel_id: int | None
    ) -> None:
        obj = await self.session.get(Client, client_id)
        if not obj:
            raise ValueError("client not found")
        obj.last_channel_id = int(channel_id) if channel_id is not None else None
        await self.session.commit()
