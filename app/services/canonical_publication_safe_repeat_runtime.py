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
    allow_repeat_time: bool = False,
    allow_repeat_time_pin: bool = False,
    allow_repeat_time_forward: bool = False,
    allow_repeat_views: bool = False,
    allow_repeat_views_pin: bool = False,
    allow_repeat_views_forward: bool = False,
    allow_repeat_views_pin_forward: bool = False,
    repeat_owner_policy_enforced: bool = False,
) -> CanonicalPublicationDeliveryRuntime:
    """Compose strict repeat delivery only from concrete dependency facts.

    Repeat+time requires repeat authority, generic time deletion, its dedicated started
    dependency, and owner policy. Time+pin and time+forward are independent narrower
    slices, each requiring its own started fact over plain repeat+time.
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
    repeat_time_enabled = bool(
        allow_repeat_time
        and repeat_enabled
        and allow_time_autodelete
    )
    repeat_time_pin_enabled = bool(
        allow_repeat_time_pin and repeat_time_enabled
    )
    repeat_time_forward_enabled = bool(
        allow_repeat_time_forward and repeat_time_enabled
    )
    repeat_views_enabled = bool(
        allow_repeat_views and repeat_enabled and allow_views_autodelete
    )
    repeat_views_pin_enabled = bool(
        allow_repeat_views_pin and repeat_views_enabled
    )
    repeat_views_forward_enabled = bool(
        allow_repeat_views_forward and repeat_views_enabled
    )
    repeat_views_pin_forward_enabled = bool(
        allow_repeat_views_pin_forward
        and repeat_views_pin_enabled
        and repeat_views_forward_enabled
    )
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
        allow_repeat_time=repeat_time_enabled,
        allow_repeat_time_pin=repeat_time_pin_enabled,
        allow_repeat_time_forward=repeat_time_forward_enabled,
        allow_repeat_views=repeat_views_enabled,
        allow_repeat_views_pin=repeat_views_pin_enabled,
        allow_repeat_views_forward=repeat_views_forward_enabled,
        allow_repeat_views_pin_forward=repeat_views_pin_forward_enabled,
    )
    return replace(
        runtime,
        executor=executor,
        post_send_hook=post_send_hook,
    )
