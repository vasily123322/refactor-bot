from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.post_task_retention import PostTaskRetentionService


class PostTaskRetentionWorker:
    """Opt-in bounded cleanup for safely canonicalized legacy scheduler rows."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: int = 3600,
        retention_days: int = 90,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.retention_days = max(7, min(int(retention_days), 3650))
        self.batch_size = max(1, min(int(batch_size), 500))
        self._loop = PollingLoop(
            interval_seconds=max(60, int(interval_seconds)),
            on_tick=self._tick,
            name="post-task-retention",
        )

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def _tick(self) -> None:
        async with self.session_factory() as session:
            tick = await PostTaskRetentionService(
                session,
                retention_days=self.retention_days,
                batch_size=self.batch_size,
            ).run_once()
        if (
            tick.deleted
            or tick.skipped_repeat
            or tick.skipped_delivery_evidence
            or tick.failures
        ):
            logger.info(
                "PostTask retention: selected={} eligible={} deleted={} "
                "skipped_repeat={} skipped_evidence={} skipped_changed={} failures={}",
                tick.selected,
                tick.eligible,
                tick.deleted,
                tick.skipped_repeat,
                tick.skipped_delivery_evidence,
                tick.skipped_changed,
                tick.failures,
            )
