"""Repository for AIAutoTask — CRUD for scheduled AI auto-tasks."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ai_auto_task import AIAutoTask


class AIAutoTaskRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_channel(self, channel_id: int) -> list[AIAutoTask]:
        """Return all auto-tasks for a channel, ordered by task_type."""
        result = await self._session.execute(
            select(AIAutoTask)
            .where(AIAutoTask.channel_id == channel_id)
            .order_by(AIAutoTask.task_type)
        )
        return list(result.scalars().all())

    async def get_by_channel_and_type(
        self, channel_id: int, task_type: str
    ) -> AIAutoTask | None:
        result = await self._session.execute(
            select(AIAutoTask).where(
                (AIAutoTask.channel_id == channel_id)
                & (AIAutoTask.task_type == task_type)
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        channel_id: int,
        task_type: str,
        *,
        enabled: bool = True,
        run_at: str = "10:00",
        schedule: str = "daily",
        day_of_week: int | None = None,
    ) -> AIAutoTask:
        """Create or update an auto-task."""
        existing = await self.get_by_channel_and_type(channel_id, task_type)
        if existing:
            existing.enabled = enabled
            existing.run_at = run_at
            existing.schedule = schedule
            existing.day_of_week = day_of_week
            await self._session.flush()
            return existing
        task = AIAutoTask(
            channel_id=channel_id,
            task_type=task_type,
            enabled=enabled,
            run_at=run_at,
            schedule=schedule,
            day_of_week=day_of_week,
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def set_enabled(
        self, channel_id: int, task_type: str, enabled: bool
    ) -> bool:
        task = await self.get_by_channel_and_type(channel_id, task_type)
        if not task:
            return False
        task.enabled = enabled
        await self._session.flush()
        return True

    async def update_last_run(
        self, channel_id: int, task_type: str, run_at: Any
    ) -> None:
        task = await self.get_by_channel_and_type(channel_id, task_type)
        if task:
            task.last_run_at = run_at
            await self._session.flush()

    async def delete(self, channel_id: int, task_type: str) -> bool:
        task = await self.get_by_channel_and_type(channel_id, task_type)
        if not task:
            return False
        await self._session.delete(task)
        await self._session.flush()
        return True
