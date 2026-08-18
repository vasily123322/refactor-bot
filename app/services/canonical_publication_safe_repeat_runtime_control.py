from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_authority import (
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.canonical_publication_safe_repeat_runtime import (
    build_canonical_publication_safe_repeat_runtime,
)
from app.workers.canonical_publication_delivery import CanonicalPublicationDeliveryWorker


async def start_canonical_publication_safe_repeat_primary_if_enabled(
    *,
    config: CanonicalPublicationDeliveryPrimarySettings,
    recovery_worker: object | None,
    bot,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    time_autodelete_executor_available: bool = False,
    views_autodelete_executor_available: bool = False,
    repeat_continuation_available: bool = False,
    repeat_time_executor_available: bool = False,
    repeat_time_pin_executor_available: bool = False,
    repeat_time_forward_executor_available: bool = False,
    repeat_time_pin_forward_executor_available: bool = False,
    repeat_views_executor_available: bool = False,
    repeat_views_pin_executor_available: bool = False,
    repeat_views_forward_executor_available: bool = False,
    repeat_views_pin_forward_executor_available: bool = False,
    repeat_owner_policy_enforced: bool = False,
) -> CanonicalPublicationDeliveryWorker | None:
    """Start repeat-aware primary only from concrete successfully started dependencies."""

    if not config.enabled:
        logger.info("Boot: canonical publication delivery worker disabled")
        return None
    if recovery_worker is None:
        raise RuntimeError(
            "canonical publication delivery requires a successfully started recovery worker"
        )

    repeat_available = bool(repeat_continuation_available)
    time_available = bool(time_autodelete_executor_available)
    repeat_time_available = bool(
        repeat_time_executor_available and repeat_available and time_available
    )
    repeat_time_pin_available = bool(
        repeat_time_pin_executor_available and repeat_time_available
    )
    repeat_time_forward_available = bool(
        repeat_time_forward_executor_available and repeat_time_available
    )
    repeat_time_pin_forward_available = bool(
        repeat_time_pin_forward_executor_available
        and repeat_time_pin_available
        and repeat_time_forward_available
    )
    views_available = bool(views_autodelete_executor_available)
    repeat_views_available = bool(
        repeat_views_executor_available and repeat_available and views_available
    )
    repeat_views_pin_available = bool(
        repeat_views_pin_executor_available and repeat_views_available
    )
    repeat_views_forward_available = bool(
        repeat_views_forward_executor_available and repeat_views_available
    )
    repeat_views_pin_forward_available = bool(
        repeat_views_pin_forward_executor_available
        and repeat_views_pin_available
        and repeat_views_forward_available
    )

    runtime = build_canonical_publication_safe_repeat_runtime(
        bot=bot,
        session_factory=session_factory,
        lease_seconds=config.lease_ttl_seconds,
        heartbeat_interval_seconds=float(config.heartbeat_interval_seconds),
        allow_time_autodelete=time_available,
        allow_views_autodelete=views_available,
        allow_repeat=repeat_available,
        allow_repeat_time=repeat_time_available,
        allow_repeat_time_pin=repeat_time_pin_available,
        allow_repeat_time_forward=repeat_time_forward_available,
        allow_repeat_time_pin_forward=repeat_time_pin_forward_available,
        allow_repeat_views=repeat_views_available,
        allow_repeat_views_pin=repeat_views_pin_available,
        allow_repeat_views_forward=repeat_views_forward_available,
        allow_repeat_views_pin_forward=repeat_views_pin_forward_available,
        repeat_owner_policy_enforced=bool(repeat_owner_policy_enforced),
    )
    handoff_executor = CanonicalPublicationRepeatHandoffExecutor(
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
                "Boot: failed to clean up safe repeat canonical publication worker after startup failure"
            )
        raise
    set_canonical_publication_delivery_primary_worker(
        worker,
        time_autodelete_available=time_available,
        views_autodelete_available=views_available,
        repeat_continuation_available=repeat_available,
        repeat_owner_policy_enforced=bool(repeat_owner_policy_enforced),
        repeat_time_available=repeat_time_available,
        repeat_time_pin_available=repeat_time_pin_available,
        repeat_time_forward_available=repeat_time_forward_available,
        repeat_time_pin_forward_available=repeat_time_pin_forward_available,
        repeat_views_available=repeat_views_available,
        repeat_views_pin_available=repeat_views_pin_available,
    )
    return worker
