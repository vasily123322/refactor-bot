from __future__ import annotations

import asyncio
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
from app.services.canonical_publication_delivery_live_post_action_executor import (
    CanonicalPublicationDeliveryPostActionExecution,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)


class CanonicalPublicationDeliveryLiveAuxiliaryExecutorLike(Protocol):
    async def execute(
        self,
        plan: CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    ) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution: ...


class CanonicalPublicationDeliveryLivePostActionExecutorLike(Protocol):
    async def execute(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> CanonicalPublicationDeliveryPostActionExecution: ...


class CanonicalPublicationDeliveryLiveAuxiliaryHook:
    """Coordinate current live admin, pin/forward, and owner auxiliaries.

    Proven runtime order is `admin -> pin/forward -> owner`. Each owner action is
    re-authorized after the intervening provider side effects. The pin/forward executor
    independently performs durable reservation plus fresh live reauthorization before
    every provider call.

    Autodelete remains intentionally outside this coordinator until its own canonical
    runtime widening is connected. Generic pin/forward failures are best-effort and do
    not suppress a later owner notice; cancellation is never swallowed.
    """

    def __init__(
        self,
        *,
        executor: CanonicalPublicationDeliveryLiveAuxiliaryExecutorLike,
        post_action_executor: CanonicalPublicationDeliveryLivePostActionExecutorLike
        | None = None,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    ) -> None:
        self.executor = executor
        self.post_action_executor = post_action_executor
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
            if admin_result.admin_failed or admin_result.invalid_plans:
                logger.info(
                    "Canonical live admin auxiliary completed with issues "
                    "publication_id={} failed={} invalid={}",
                    int(context.publication_id),
                    int(admin_result.admin_failed),
                    int(admin_result.invalid_plans),
                )

        if self.post_action_executor is not None:
            try:
                action_result = await self.post_action_executor.execute(context)
                if (
                    action_result.unknown
                    or action_result.suppressed
                    or action_result.conflicts
                ):
                    logger.info(
                        "Canonical live post actions completed with issues "
                        "publication_id={} unknown={} suppressed={} conflicts={}",
                        int(context.publication_id),
                        int(action_result.unknown),
                        int(action_result.suppressed),
                        int(action_result.conflicts),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Canonical live post-action coordinator failed "
                    "publication_id={} error_type={}",
                    int(context.publication_id),
                    type(exc).__name__,
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
            if owner_result.owner_failed or owner_result.invalid_plans:
                logger.info(
                    "Canonical live owner auxiliary completed with issues "
                    "publication_id={} failed={} invalid={}",
                    int(context.publication_id),
                    int(owner_result.owner_failed),
                    int(owner_result.invalid_plans),
                )
