from __future__ import annotations

from loguru import logger

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.scheduler_recovery import SchedulerRecoveryTick, SchedulerTaskRecoveryService


class SchedulerRecoveryWorker:
    """Bounded fail-closed recovery for expired scheduler execution leases."""

    def __init__(
        self,
        *,
        session_factory=AsyncSessionLocal,
        interval_seconds: int = 60,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.batch_size = max(1, min(int(batch_size), 500))
        self._service = SchedulerTaskRecoveryService(session_factory)
        self._loop = PollingLoop(
            interval_seconds=max(1, int(interval_seconds)),
            on_tick=self._tick,
            name="scheduler-recovery",
        )

    async def run_once(self) -> SchedulerRecoveryTick:
        return await self._service.run_once(batch_size=self.batch_size)

    async def _tick(self) -> None:
        tick = await self.run_once()
        if tick.selected:
            logger.info(
                "Scheduler recovery: selected={} taken_over={} confirmed={} unknown_failed={} terminal_cleaned={} orphaned={} contention={} failures={}",
                tick.selected,
                tick.taken_over,
                tick.confirmed_published,
                tick.failed_unknown,
                tick.terminal_cleaned,
                tick.orphaned,
                tick.contention,
                tick.failures,
            )

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()
