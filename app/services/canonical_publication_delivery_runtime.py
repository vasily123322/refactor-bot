from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_delivery_live_autodelete import (
    CanonicalPublicationDeliveryLiveAutodeleteWriter,
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
from app.services.canonical_publication_repeat_delivery_executor import (
    CanonicalPublicationRepeatDeliveryExecutor,
)
from app.services.canonical_publication_result_link import (
    CanonicalPublicationResultLinkResolver,
)
from app.services.document_posting import DocumentPostingService


class CanonicalPublicationDeliveryLiveAutodeleteCoordinator:
    """Open one short DB session per live timer materialization request."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.session_factory = session_factory

    async def materialize(self, context):
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryLiveAutodeleteWriter(
                session
            ).materialize(context)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryRuntime:
    """Concrete dependencies for canonical primary delivery, without worker startup."""

    executor: CanonicalPublicationDeliveryExecutor
    sender: DocumentPostingService
    result_link_resolver: CanonicalPublicationResultLinkResolver
    auxiliary_executor: CanonicalPublicationDeliveryLiveAuxiliaryExecutor
    autodelete_writer: CanonicalPublicationDeliveryLiveAutodeleteCoordinator
    post_action_executor: CanonicalPublicationDeliveryLivePostActionExecutor
    post_send_hook: CanonicalPublicationDeliveryLiveAuxiliaryHook


def build_canonical_publication_delivery_runtime(
    *,
    bot,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    holder: str = "canonical-publication-delivery",
    lease_seconds: int = 180,
    heartbeat_interval_seconds: float = 45.0,
    allow_time_autodelete: bool = False,
    allow_views_autodelete: bool = False,
    allow_repeat: bool = False,
) -> CanonicalPublicationDeliveryRuntime:
    """Compose canonical delivery dependencies without starting provider-capable work.

    Construction is side-effect free. Delete and repeat authority are independent and
    default off. Repeat affects claim eligibility only; successor continuation remains a
    separate provider-free worker dependency.
    """

    sender = DocumentPostingService(bot, session_factory)
    result_link_resolver = CanonicalPublicationResultLinkResolver(bot)
    auxiliary_executor = CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot)
    autodelete_writer = CanonicalPublicationDeliveryLiveAutodeleteCoordinator(
        session_factory=session_factory,
    )
    post_action_executor = CanonicalPublicationDeliveryLivePostActionExecutor(
        bot=bot,
        session_factory=session_factory,
    )
    post_send_hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=auxiliary_executor,
        autodelete_writer=autodelete_writer,
        post_action_executor=post_action_executor,
        session_factory=session_factory,
    )
    executor = CanonicalPublicationRepeatDeliveryExecutor(
        session_factory,
        sender=sender,
        result_link_resolver=result_link_resolver,
        post_send_hook=post_send_hook,
        holder=holder,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        allow_time_autodelete=allow_time_autodelete,
        allow_views_autodelete=allow_views_autodelete,
        allow_repeat=allow_repeat,
    )
    return CanonicalPublicationDeliveryRuntime(
        executor=executor,
        sender=sender,
        result_link_resolver=result_link_resolver,
        auxiliary_executor=auxiliary_executor,
        autodelete_writer=autodelete_writer,
        post_action_executor=post_action_executor,
        post_send_hook=post_send_hook,
    )
