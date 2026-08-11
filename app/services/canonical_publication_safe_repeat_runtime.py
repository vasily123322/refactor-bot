from __future__ import annotations

from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)
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
    allow_repeat_views: bool = False,
    repeat_owner_policy_enforced: bool = False,
) -> CanonicalPublicationDeliveryRuntime:
    """Compose strict repeat delivery only from concrete dependency facts.

    The established delivery builder remains the source of sender/result-link/auxiliary,
    timer and post-action dependencies. This composition then rebuilds only the live hook
    and primary executor so the owner-policy fact corresponds to actual enforcement at
    the final owner provider boundary rather than a declarative boolean.

    Repeat authority requires both successor continuation availability and the enforced
    owner policy. Non-repeat time/views capability facts remain independent. Repeat+views
    is a third, default-off composition fact and is never inferred merely because repeat
    continuation and a views worker are independently available.
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
    owner_policy_enforced = bool(repeat_owner_policy_enforced)
    post_send_hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=runtime.auxiliary_executor,
        autodelete_writer=runtime.autodelete_writer,
        post_action_executor=runtime.post_action_executor,
        session_factory=session_factory,
        repeat_owner_policy_enforced=owner_policy_enforced,
    )
    repeat_enabled = bool(allow_repeat and owner_policy_enforced)
    executor = CanonicalPublicationSafeRepeatDeliveryExecutor(
        session_factory,
        sender=runtime.sender,
        result_link_resolver=runtime.result_link_resolver,
        post_send_hook=post_send_hook,
        holder=holder,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        allow_time_autodelete=allow_time_autodelete,
        allow_views_autodelete=allow_views_autodelete,
        allow_repeat=repeat_enabled,
        allow_repeat_views=bool(allow_repeat_views and repeat_enabled),
    )
    return replace(
        runtime,
        executor=executor,
        post_send_hook=post_send_hook,
    )
