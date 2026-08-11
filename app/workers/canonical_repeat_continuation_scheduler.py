from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.workers.canonical_recovery_scheduler import Scheduler as RecoveryScheduler
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker


class Scheduler(RecoveryScheduler):
    """Recovery scheduler plus provider-free canonical repeat continuation lifecycle.

    The existing successful-repeat flag remains the single operator intent. Legacy-linked
    successful repeats continue through the inherited scheduler callback; transport-
    retired canonical-delivered repeat sources are disjoint and handled by the child
    continuation worker.

    Production constructs the scheduler with an async session factory. A single long-lived
    AsyncSession cannot safely back an independent polling worker, so continuation stays
    unavailable in that unsupported construction shape rather than sharing a session.
    """

    def __init__(
        self,
        *args,
        repeat_continuation_enabled: bool | None = None,
        continuation_worker_factory: Callable[..., CanonicalRepeatContinuationWorker] = (
            CanonicalRepeatContinuationWorker
        ),
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._repeat_continuation_enabled = (
            bool(settings.canonical_repeat_successful_planning_enabled)
            if repeat_continuation_enabled is None
            else bool(repeat_continuation_enabled)
        )
        self._continuation_worker_factory = continuation_worker_factory
        self._repeat_continuation_worker: CanonicalRepeatContinuationWorker | None = None

    @property
    def repeat_continuation_available(self) -> bool:
        return self._repeat_continuation_worker is not None

    def _continuation_session_factory(
        self,
    ) -> async_sessionmaker[AsyncSession] | None:
        factory = getattr(self, "session_factory", None)
        return factory if factory is not None else None

    async def start(self) -> None:
        await super().start()
        if not self._repeat_continuation_enabled:
            return

        session_factory = self._continuation_session_factory()
        if session_factory is None:
            logger.warning(
                "Canonical repeat continuation requested but scheduler has no session factory"
            )
            return

        worker = self._continuation_worker_factory(session_factory=session_factory)
        try:
            await worker.start()
        except BaseException:
            try:
                await worker.stop()
            except Exception:
                logger.exception(
                    "Boot: failed to clean up canonical repeat continuation worker after startup failure"
                )
            await super().stop()
            raise
        self._repeat_continuation_worker = worker

    async def stop(self) -> None:
        worker = self._repeat_continuation_worker
        self._repeat_continuation_worker = None
        if worker is not None:
            try:
                await worker.stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shutdown: failed to stop canonical repeat continuation")
        await super().stop()
