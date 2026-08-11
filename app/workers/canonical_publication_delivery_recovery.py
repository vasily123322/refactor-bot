from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.services.canonical_publication_delivery_recovery import (
    CanonicalPublicationDeliveryRecoveryService,
    CanonicalPublicationDeliveryRecoveryTick,
)


def _bounded_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


class CanonicalPublicationDeliveryRecoveryWorker:
    """Polling wrapper for fail-closed expired canonical delivery recovery.

    The worker has no Telegram/provider dependency. Each tick delegates to the bounded
    recovery service, which only takes expired typed delivery leases and closes ambiguous
    ``sending`` state as unknown delivery. It never retries the original send.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: int = 60,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.batch_size = _bounded_int(
            batch_size,
            default=100,
            minimum=1,
            maximum=500,
        )
        self.interval_seconds = _bounded_int(
            interval_seconds,
            default=60,
            minimum=30,
            maximum=86400,
        )
        self._loop = PollingLoop(
            interval_seconds=self.interval_seconds,
            on_tick=self._tick,
            name="canonical-publication-delivery-recovery",
        )

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def run_once(self) -> CanonicalPublicationDeliveryRecoveryTick:
        return await CanonicalPublicationDeliveryRecoveryService(
            self.session_factory
        ).run_once(batch_size=self.batch_size)

    async def _tick(self) -> None:
        tick = await self.run_once()
        if (
            tick.taken_over
            or tick.contention
            or tick.conflicts
            or tick.failures
        ):
            logger.info(
                "Canonical publication delivery recovery: selected={} taken_over={} "
                "failed_unknown={} contention={} conflicts={} failures={}",
                tick.selected,
                tick.taken_over,
                tick.failed_unknown,
                tick.contention,
                tick.conflicts,
                tick.failures,
            )
