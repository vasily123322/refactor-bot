from sqlalchemy import select, delete, update
from sqlalchemy import and_, or_
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import JoinRequest


class JoinRequestsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_pending(
        self, channel_id: int, limit: int = 20, offset: int = 0
    ) -> list[JoinRequest]:
        res = await self.session.execute(
            select(JoinRequest)
            .where(
                JoinRequest.channel_id == channel_id, JoinRequest.status == "pending"
            )
            .order_by(JoinRequest.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(res.scalars().all())

    async def set_status(self, channel_id: int, user_id: int, status: str) -> None:
        await self.session.execute(
            update(JoinRequest)
            .where(JoinRequest.channel_id == channel_id, JoinRequest.user_id == user_id)
            .values(status=status)
        )
        await self.session.commit()

    async def get(self, channel_id: int, user_id: int) -> JoinRequest | None:
        res = await self.session.execute(
            select(JoinRequest).where(
                JoinRequest.channel_id == channel_id, JoinRequest.user_id == user_id
            )
        )
        return res.scalars().first()

    async def delete(self, channel_id: int, user_id: int) -> None:
        await self.session.execute(
            delete(JoinRequest).where(
                JoinRequest.channel_id == channel_id, JoinRequest.user_id == user_id
            )
        )
        await self.session.commit()

    async def list_pending_solved(
        self, channel_id: int, limit: int
    ) -> list[JoinRequest]:
        # solved: либо нет челенджа, либо attempts_left == 0
        res = await self.session.execute(
            select(JoinRequest)
            .where(
                JoinRequest.channel_id == channel_id,
                JoinRequest.status == "pending",
                (
                    (JoinRequest.challenge_type.is_(None))
                    | (JoinRequest.attempts_left == 0)
                ),
            )
            .order_by(JoinRequest.created_at.asc())
            .limit(limit)
        )
        return list(res.scalars().all())

    async def list_pending_filtered(
        self,
        channel_id: int,
        solved_only: bool,
        invite_like: str | None,
        minutes: int | None,
        limit: int,
        offset: int,
    ) -> list[JoinRequest]:
        conds = [
            JoinRequest.channel_id == channel_id,
            JoinRequest.status == "pending",
        ]
        if solved_only:
            conds.append(
                or_(
                    JoinRequest.challenge_type.is_(None), JoinRequest.attempts_left == 0
                )
            )
        if invite_like:
            like = f"%{invite_like}%"
            conds.append(JoinRequest.invite_link.like(like))
        if minutes and minutes > 0:
            conds.append(
                JoinRequest.created_at
                > (datetime.now(timezone.utc) - timedelta(minutes=int(minutes)))
            )
        res = await self.session.execute(
            select(JoinRequest)
            .where(and_(*conds))
            .order_by(JoinRequest.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(res.scalars().all())
