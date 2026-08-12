from __future__ import annotations

from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecution,
)
from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    CanonicalPublicationDeliveryLiveAuxiliaryPlanner,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)


class CanonicalPublicationDeliveryLiveAuxiliaryExecutorLike(Protocol):
    async def execute(
        self,
        plan: CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    ) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution: ...


class CanonicalPublicationDeliveryLiveAuxiliaryHook:
    """One-shot post-send coordinator for current canonical owner/admin parity.

    Each non-idempotent provider action is preceded by a fresh planner pass in a fresh
    short DB session. That rechecks exact live lease ownership and the full claimed intent
    immediately before the action. Admin logging executes first, matching legacy order;
    owner notification is independently re-authorized afterwards.

    The hook never retries an action. If ownership or intent is lost between admin and
    owner, the second planner returns no plan and owner notification is skipped. Generic
    DB/provider failures may bubble to the outer best-effort post-send hook boundary;
    cancellation is never swallowed.
    """

    def __init__(
        self,
        *,
        executor: CanonicalPublicationDeliveryLiveAuxiliaryExecutorLike,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self.executor = executor
        self.session_factory = session_factory

    async def _plan(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> CanonicalPublicationDeliveryLiveAuxiliaryPlan | None:
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(session).plan(
                context
            )

    async def execute(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> None:
        first = await self._plan(context)
        if first is None:
            logger.info(
                "Canonical live auxiliaries not authorized before admin "
                "publication_id={}",
                int(context.publication_id),
            )
            return

        if first.admin_log is not None:
            admin_result = await self.executor.execute(
                CanonicalPublicationDeliveryLiveAuxiliaryPlan(
                    publication_id=int(first.publication_id),
                    owner_notice=None,
                    admin_log=first.admin_log,
                )
            )
            if (
                admin_result.admin_failed
                or admin_result.invalid_plans
            ):
                logger.info(
                    "Canonical live admin auxiliary completed with issues "
                    "publication_id={} failed={} invalid={}",
                    int(context.publication_id),
                    int(admin_result.admin_failed),
                    int(admin_result.invalid_plans),
                )

        second = await self._plan(context)
        if second is None:
            logger.info(
                "Canonical live auxiliaries not authorized before owner "
                "publication_id={}",
                int(context.publication_id),
            )
            return

        if second.owner_notice is not None:
            owner_result = await self.executor.execute(
                CanonicalPublicationDeliveryLiveAuxiliaryPlan(
                    publication_id=int(second.publication_id),
                    owner_notice=second.owner_notice,
                    admin_log=None,
                )
            )
            if (
                owner_result.owner_failed
                or owner_result.invalid_plans
            ):
                logger.info(
                    "Canonical live owner auxiliary completed with issues "
                    "publication_id={} failed={} invalid={}",
                    int(context.publication_id),
                    int(owner_result.owner_failed),
                    int(owner_result.invalid_plans),
                )
