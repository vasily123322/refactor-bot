from __future__ import annotations

from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.core.db import AsyncSessionLocal
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
) -> CanonicalPublicationDeliveryWorker | None:
    """Start primary delivery only after recovery has successfully started.

    The caller passes the concrete recovery worker returned only after its awaited
    ``start()`` completed. A missing object therefore fails closed before runtime
    composition or provider-capable worker construction.

    The polling worker receives a handoff-gated executor rather than the concrete
    provider executor directly. Linked legacy rows must first commit the atomic
    PostTask retirement seam; canonical-only rows delegate without a handoff write.
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
    return worker


async def stop_canonical_publication_delivery_workers(
    *,
    primary_worker: _StoppableWorker | None,
    recovery_worker: _StoppableWorker | None,
) -> None:
    """Stop canonical delivery in dependency order: primary first, recovery last."""

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
