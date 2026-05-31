from __future__ import annotations
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.models import AICustomSystemPrompt


class CustomSystemPromptsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_by_channel(self, channel_id: int) -> list[AICustomSystemPrompt]:
        res = await self.session.execute(
            select(AICustomSystemPrompt)
            .where(AICustomSystemPrompt.channel_id == channel_id)
            .order_by(AICustomSystemPrompt.id.desc())
        )
        return list(res.scalars().all())

    async def get_active(self, channel_id: int) -> AICustomSystemPrompt | None:
        res = await self.session.execute(
            select(AICustomSystemPrompt).where(
                AICustomSystemPrompt.channel_id == channel_id,
                AICustomSystemPrompt.is_active.is_(True),
            )
        )
        return res.scalars().first()

    async def create(
        self, channel_id: int, content: str, *, active: bool = False
    ) -> AICustomSystemPrompt:
        obj = AICustomSystemPrompt(
            channel_id=channel_id, content=content, is_active=bool(active)
        )
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj

    async def set_active(self, channel_id: int, prompt_id: int) -> None:
        # deactivate all
        await self.session.execute(
            update(AICustomSystemPrompt)
            .where(AICustomSystemPrompt.channel_id == channel_id)
            .values(is_active=False)
        )
        # activate one
        obj = await self.session.get(AICustomSystemPrompt, prompt_id)
        if obj and obj.channel_id == channel_id:
            obj.is_active = True
        await self.session.commit()

    async def update_content(self, prompt_id: int, content: str) -> bool:
        obj = await self.session.get(AICustomSystemPrompt, prompt_id)
        if not obj:
            return False
        obj.content = content
        await self.session.commit()
        return True

    async def delete(self, prompt_id: int) -> bool:
        obj = await self.session.get(AICustomSystemPrompt, prompt_id)
        if not obj:
            return False
        await self.session.delete(obj)
        await self.session.commit()
        return True
