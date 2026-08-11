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
    post_send_hook: CanonicalPublicationDeliveryLiveAuxiliaryHook


def build_canonical_publication_delivery_runtime(
    *,
    bot,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    holder: str = "canonical-publication-delivery",
    lease_seconds: int = 180,
    heartbeat_interval_seconds: float = 45.0,
) -> CanonicalPublicationDeliveryRuntime:
    """Compose the current plain canonical delivery slice without starting work.

    Construction is side-effect free: no database session is opened and no Telegram
    method is called. The returned executor still enforces #185 capability restrictions
    (empty runtime options and non-repeat), while result-link and live owner/admin parity
    are wired through the exact post-send lease lifecycle from #199/#207-#209.
    """

    sender = DocumentPostingService(bot, session_factory)
    result_link_resolver = CanonicalPublicationResultLinkResolver(bot)
    auxiliary_executor = CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot)
    post_send_hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=auxiliary_executor,
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
        post_send_hook=post_send_hook,
    )
