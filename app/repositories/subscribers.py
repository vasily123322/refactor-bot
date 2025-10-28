from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import Subscriber
from app.repositories.base import BaseRepository


class SubscribersRepo(BaseRepository[Subscriber]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Subscriber)

    async def get(self, channel_id: int, user_id: int) -> Subscriber | None:
        res = await self.session.execute(
            select(Subscriber).where(Subscriber.channel_id == channel_id, Subscriber.user_id == user_id)
        )
        return res.scalars().first()

    async def add(self, channel_id: int, user_id: int, username: str | None, full_name: str | None) -> Subscriber:
        obj = await self.get(channel_id, user_id)
        if obj:
            return obj
        obj = Subscriber(channel_id=channel_id, user_id=user_id, username=username, full_name=full_name, tags=[])
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj

    async def list_user_ids(self, channel_id: int) -> list[int]:
        res = await self.session.execute(select(Subscriber.user_id).where(Subscriber.channel_id == channel_id))
        return [int(r[0]) for r in res.all()]

    async def add_tag(self, channel_id: int, user_id: int, tag: str) -> None:
        obj = await self.get(channel_id, user_id)
        if not obj:
            return
        cur = list(getattr(obj, "tags", []) or [])
        if tag not in cur:
            cur.append(tag)
            obj.tags = cur
            await self.session.commit()

    async def list_user_ids_by_tag(self, channel_id: int, tag: str) -> list[int]:
        res = await self.session.execute(select(Subscriber).where(Subscriber.channel_id == channel_id))
        uids: list[int] = []
        for s in res.scalars().all():
            try:
                if tag in (s.tags or []):
                    uids.append(int(s.user_id))
            except Exception:
                continue
        return uids

    async def list_all_by_channel(self, channel_id: int) -> list[Subscriber]:
        res = await self.session.execute(select(Subscriber).where(Subscriber.channel_id == channel_id).order_by(Subscriber.created_at.desc()))
        return list(res.scalars().all())



