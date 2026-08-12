from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecutor,
)
from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)
from app.services.canonical_publication_delivery_live_post_action_executor import (
    CanonicalPublicationDeliveryLivePostActionExecutor,
)
from app.services.canonical_publication_result_link import (
    CanonicalPublicationResultLinkResolver,
)
from app.services.document_posting import DocumentPostingService


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryRuntime:
    """Concrete dependencies for canonical primary delivery, without worker startup."""

    executor: CanonicalPublicationDeliveryExecutor
    sender: DocumentPostingService
    result_link_resolver: CanonicalPublicationResultLinkResolver
    auxiliary_executor: CanonicalPublicationDeliveryLiveAuxiliaryExecutor
    post_action_executor: CanonicalPublicationDeliveryLivePostActionExecutor
    post_send_hook: CanonicalPublicationDeliveryLiveAuxiliaryHook


def build_canonical_publication_delivery_runtime(
    *,
    bot,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    holder: str = "canonical-publication-delivery",
    lease_seconds: int = 180,
    heartbeat_interval_seconds: float = 45.0,
) -> CanonicalPublicationDeliveryRuntime:
    """Compose the current canonical delivery slice without starting work.

    Construction is side-effect free: no database session is opened and no Telegram
    method is called. Capability authority remains inside the locked claim/handoff path;
    live owner/admin and durable no-retry pin/forward actions run only inside the exact
    post-send delivery lease lifecycle.
    """

    sender = DocumentPostingService(bot, session_factory)
    result_link_resolver = CanonicalPublicationResultLinkResolver(bot)
    auxiliary_executor = CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot)
    post_action_executor = CanonicalPublicationDeliveryLivePostActionExecutor(
        bot=bot,
        session_factory=session_factory,
    )
    post_send_hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=auxiliary_executor,
        post_action_executor=post_action_executor,
        session_factory=session_factory,
    )
    executor = CanonicalPublicationDeliveryExecutor(
        session_factory,
        sender=sender,
        result_link_resolver=result_link_resolver,
        post_send_hook=post_send_hook,
        holder=holder,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
    return CanonicalPublicationDeliveryRuntime(
        executor=executor,
        sender=sender,
        result_link_resolver=result_link_resolver,
        auxiliary_executor=auxiliary_executor,
        post_action_executor=post_action_executor,
        post_send_hook=post_send_hook,
    )
