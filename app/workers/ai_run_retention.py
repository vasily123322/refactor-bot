from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.ai_run_retention import AIRunRetentionService


class AIRunRetentionWorker:
    """Run conservative AI provenance retention on startup and once per day."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
        interval_seconds: float = 24 * 60 * 60,
    ) -> None:
        self.session_factory = session_factory
        self.interval_seconds = max(60.0, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="ai-run-retention")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _tick(self) -> None:
        async with self.session_factory() as session:
            result = await AIRunRetentionService(session).cleanup()
        if result.total_deleted:
            logger.info(
                "AI run retention removed terminal provenance candidates={} enrichment={} rewrite={}",
                result.candidates_scanned,
                result.enrichment_deleted,
                result.rewrite_deleted,
            )

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("AI run retention tick failed error_type={}", type(exc).__name__)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except asyncio.TimeoutError:
                continue
