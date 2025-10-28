from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import Channel, Client, ChannelSettings
from app.repositories.base import BaseRepository

class ChannelsRepo(BaseRepository[Channel]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Channel)

    async def get_by_chat_id(self, chat_id: int) -> Channel | None:
        res = await self.session.execute(select(Channel).where(Channel.tg_chat_id == chat_id))
        return res.scalars().first()

    async def create(self, owner_id: int, tg_chat_id: int, title: str | None) -> Channel:
        channel = Channel(owner_id=owner_id, tg_chat_id=tg_chat_id, title=title)
        self.session.add(channel)
        await self.session.flush()
        self.session.add(ChannelSettings(channel_id=channel.id, autosign=None, split_rules=[]))
        await self.session.commit()
        await self.session.refresh(channel)
        return channel

    async def count_by_owner(self, owner_id: int) -> int:
        res = await self.session.execute(select(func.count()).select_from(Channel).where(Channel.owner_id == owner_id))
        return int(res.scalar_one())

    async def list_by_owner(self, owner_id: int) -> list[Channel]:
        res = await self.session.execute(select(Channel).where(Channel.owner_id == owner_id).order_by(Channel.created_at.desc()))
        return list(res.scalars().all())

    async def delete_by_id(self, channel_id: int) -> bool:
        obj = await self.session.get(Channel, channel_id)
        if not obj:
            return False
        await self.session.delete(obj)
        await self.session.commit()
        return True

    async def get_by_id(self, channel_id: int) -> Channel | None:
        return await self.session.get(Channel, channel_id)

    async def assign_owner_and_title(self, channel_id: int, owner_id: int, title: str | None) -> bool:
        obj = await self.session.get(Channel, channel_id)
        if not obj:
            return False
        obj.owner_id = owner_id
        if title and (not obj.title):
            obj.title = title
        await self.session.commit()
        return True