from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.domain.models import ModLog


class ModLogRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def write(
        self,
        channel_id: int,
        action: str,
        user_id: int | None,
        moderator_id: int | None,
        meta: dict | None = None,
    ) -> None:
        log = ModLog(
            channel_id=channel_id,
            action=action,
            user_id=user_id,
            moderator_id=moderator_id,
            meta=meta or {},
        )
        self.session.add(log)
        await self.session.commit()

    async def list_last(self, channel_id: int, limit: int = 50) -> list[ModLog]:
        res = await self.session.execute(
            select(ModLog)
            .where(ModLog.channel_id == channel_id)
            .order_by(ModLog.created_at.desc())
            .limit(limit)
        )
        return list(res.scalars().all())
