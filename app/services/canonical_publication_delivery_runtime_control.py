from __future__ import annotations

from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_authority import (
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_delivery_handoff_executor import (
    CanonicalPublicationDeliveryHandoffExecutor,
)
from app.services.canonical_publication_delivery_runtime import (
    build_canonical_publication_delivery_runtime,
)
from app.workers.canonical_publication_delivery import (
    CanonicalPublicationDeliveryWorker,
)


class _StoppableWorker(Protocol):
    async def stop(self) -> None: ...


async def start_canonical_publication_delivery_primary_if_enabled(
    *,
    config: CanonicalPublicationDeliveryPrimarySettings,
    recovery_worker: object | None,
    bot,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    time_autodelete_executor_available: bool = False,
    views_autodelete_executor_available: bool = False,
    repeat_continuation_available: bool = False,
) -> CanonicalPublicationDeliveryWorker | None:
    """Start primary only after recovery and optional dependencies are proven.

    Recovery is mandatory for every enabled primary worker. Time/views delete and repeat
    authority remain independent: each is admitted only when the caller passes the fact
    that its corresponding canonical worker successfully started.
    """

    if not config.enabled:
        logger.info("Boot: canonical publication delivery worker disabled")
        return None

    if recovery_worker is None:
        raise RuntimeError(
            "canonical publication delivery requires a successfully started recovery worker"
        )

    runtime = build_canonical_publication_delivery_runtime(
        bot=bot,
        session_factory=session_factory,
        lease_seconds=config.lease_ttl_seconds,
        heartbeat_interval_seconds=float(config.heartbeat_interval_seconds),
        allow_time_autodelete=bool(time_autodelete_executor_available),
        allow_views_autodelete=bool(views_autodelete_executor_available),
        allow_repeat=bool(repeat_continuation_available),
    )
    handoff_executor = CanonicalPublicationDeliveryHandoffExecutor(
        executor=runtime.executor,
        session_factory=session_factory,
    )
    worker = CanonicalPublicationDeliveryWorker(
        executor=handoff_executor,
        session_factory=session_factory,
        interval_seconds=config.interval_seconds,
        batch_size=config.batch_size,
        scan_limit=config.scan_limit,
    )
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up canonical publication delivery worker after startup failure"
            )
        raise
    set_canonical_publication_delivery_primary_worker(worker)
    return worker


async def stop_canonical_publication_delivery_workers(
    *,
    primary_worker: _StoppableWorker | None,
    recovery_worker: _StoppableWorker | None,
) -> None:
    """Stop canonical delivery in dependency order: primary first, recovery last."""

    if primary_worker is not None:
        set_canonical_publication_delivery_primary_worker(None)

    for name, worker in (
        ("canonical publication delivery", primary_worker),
        ("canonical publication delivery recovery", recovery_worker),
    ):
        if worker is None:
            continue
        try:
            await worker.stop()
        except Exception:
            logger.exception("Shutdown: failed to stop {}", name)
