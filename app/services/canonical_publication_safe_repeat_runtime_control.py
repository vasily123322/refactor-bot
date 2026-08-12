from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.core.db import AsyncSessionLocal
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
    repeat_views_executor_available: bool = False,
    repeat_owner_policy_enforced: bool = False,
) -> CanonicalPublicationDeliveryWorker | None:
    """Start a repeat-aware primary only after every authority dependency is explicit.

    Recovery remains mandatory for the primary itself. Repeat requires both continuation
    and enforced owner policy. Repeat+views has a third independent fact supplied only by
    a successfully started repeat-capable views worker; this function never derives that
    fact from config or from ordinary views availability.
    """

    if not config.enabled:
        logger.info("Boot: canonical publication delivery worker disabled")
        return None
    if recovery_worker is None:
        raise RuntimeError(
            "canonical publication delivery requires a successfully started recovery worker"
        )

    runtime = build_canonical_publication_safe_repeat_runtime(
        bot=bot,
        session_factory=session_factory,
        lease_seconds=config.lease_ttl_seconds,
        heartbeat_interval_seconds=float(config.heartbeat_interval_seconds),
        allow_time_autodelete=bool(time_autodelete_executor_available),
        allow_views_autodelete=bool(views_autodelete_executor_available),
        allow_repeat=bool(repeat_continuation_available),
        allow_repeat_views=bool(repeat_views_executor_available),
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
    return worker
