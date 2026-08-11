from __future__ import annotations

from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_runtime import (
    CanonicalPublicationDeliveryRuntime,
    build_canonical_publication_delivery_runtime,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)


def build_canonical_publication_safe_repeat_runtime(
    *,
    bot,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    holder: str = "canonical-publication-delivery",
    lease_seconds: int = 180,
    heartbeat_interval_seconds: float = 45.0,
    allow_time_autodelete: bool = False,
    allow_views_autodelete: bool = False,
    allow_repeat: bool = False,
    repeat_owner_policy_enforced: bool = False,
) -> CanonicalPublicationDeliveryRuntime:
    """Compose the strict repeat runtime without silently activating repeat authority.

    The existing delivery builder remains the source of sender/result-link/auxiliary/timer
    and post-action dependencies. Its executor is replaced with the strict repeat executor
    so repeat rows cannot inherit non-repeat effect capabilities.

    Repeat authority additionally requires an explicit owner-policy integration fact.
    Until the live hook actually suppresses owner publication notices for repeat, passing
    only `allow_repeat=True` is insufficient and the effective executor gate remains
    false. This prevents a future dispatcher change from activating repeat merely because
    continuation recovery is available.
    """

    runtime = build_canonical_publication_delivery_runtime(
        bot=bot,
        session_factory=session_factory,
        holder=holder,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        allow_time_autodelete=allow_time_autodelete,
        allow_views_autodelete=allow_views_autodelete,
        allow_repeat=False,
    )
    repeat_enabled = bool(allow_repeat and repeat_owner_policy_enforced)
    executor = CanonicalPublicationSafeRepeatDeliveryExecutor(
        session_factory,
        sender=runtime.sender,
        result_link_resolver=runtime.result_link_resolver,
        post_send_hook=runtime.post_send_hook,
        holder=holder,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        allow_time_autodelete=allow_time_autodelete,
        allow_views_autodelete=allow_views_autodelete,
        allow_repeat=repeat_enabled,
    )
    return replace(runtime, executor=executor)
