from __future__ import annotations

import asyncio
from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_delivery_live_autodelete import (
    CanonicalPublicationDeliveryLiveAutodeleteResult,
)
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
    CanonicalPublicationDeliveryPostSendBlockingError,
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.canonical_publication_owner_notification_policy import (
    CanonicalPublicationOwnerNotificationPolicy,
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


class CanonicalPublicationDeliveryLiveAutodeleteWriterLike(Protocol):
    async def materialize(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> CanonicalPublicationDeliveryLiveAutodeleteResult: ...


class CanonicalPublicationDeliveryLiveAuxiliaryHook:
    """Coordinate live admin, timer materialization, pin/forward, and owner actions.

    Historical non-repeat order is preserved as
    `admin -> autodelete scheduling -> pin/forward -> owner`.

    Admin, pin/forward and owner provider effects remain best-effort under their existing
    no-retry boundaries. Requested time-autodelete is different: the durable runtime
    token is required execution semantics. If it cannot be established while exact
    primary ownership is live, this hook raises a blocking error so the already-sent
    primary delivery remains ambiguous for recovery instead of being finalized without
    its requested deletion timer.

    When repeat owner policy enforcement is enabled, the final owner provider boundary
    additionally requires the strict repeat-rule policy. This is deliberately checked
    after the second live reauthorization so malformed/future repeat semantics can never
    widen into an owner notification while admin/timer/post-actions keep their existing
    independent behavior.
    """

    def __init__(
        self,
        *,
        executor: CanonicalPublicationDeliveryLiveAuxiliaryExecutorLike,
        autodelete_writer: CanonicalPublicationDeliveryLiveAutodeleteWriterLike
        | None = None,
        post_action_executor: CanonicalPublicationDeliveryLivePostActionExecutorLike
        | None = None,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        repeat_owner_policy_enforced: bool = False,
    ) -> None:
        self.executor = executor
        self.autodelete_writer = autodelete_writer
        self.post_action_executor = post_action_executor
        self.session_factory = session_factory
        self.repeat_owner_policy_enforced = bool(repeat_owner_policy_enforced)

    async def _plan(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> CanonicalPublicationDeliveryLiveAuxiliaryPlan | None:
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(session).plan(
                context
            )

    @staticmethod
    def _time_autodelete_requested(
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> bool:
        try:
            capability = parse_canonical_publication_delivery_runtime_capability(
                context.plan.runtime_options()
            )
        except (AttributeError, TypeError, ValueError):
            capability = None
        return bool(capability is not None and capability.time_autodelete_requested)

    def _owner_notice_authorized(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> bool:
        if not self.repeat_owner_policy_enforced:
            return True
        decision = CanonicalPublicationOwnerNotificationPolicy.decide(context.plan)
        if decision.owner_notice_allowed:
            return True
        logger.info(
            "Canonical live owner notice suppressed by repeat policy "
            "publication_id={} outcome={} repeat_enabled={}",
            int(context.publication_id),
            str(decision.outcome),
            bool(decision.repeat_enabled),
        )
        return False

    async def _materialize_autodelete(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> None:
        requested = self._time_autodelete_requested(context)
        if self.autodelete_writer is None:
            if requested:
                raise CanonicalPublicationDeliveryPostSendBlockingError(
                    "canonical time-autodelete writer unavailable"
                )
            return

        try:
            result = await self.autodelete_writer.materialize(context)
        except asyncio.CancelledError:
            raise
        except CanonicalPublicationDeliveryPostSendBlockingError:
            raise
        except Exception as exc:
            if requested:
                logger.warning(
                    "Canonical live autodelete materialization failed "
                    "publication_id={} error_type={}",
                    int(context.publication_id),
                    type(exc).__name__,
                )
                raise CanonicalPublicationDeliveryPostSendBlockingError(
                    "canonical time-autodelete materialization failed"
                ) from None
            logger.warning(
                "Canonical live autodelete no-op failed publication_id={} error_type={}",
                int(context.publication_id),
                type(exc).__name__,
            )
            return

        if result.outcome in {"not_requested", "created", "existing"}:
            return
        if requested:
            logger.warning(
                "Canonical live autodelete not established publication_id={} outcome={}",
                int(context.publication_id),
                str(result.outcome),
            )
            raise CanonicalPublicationDeliveryPostSendBlockingError(
                "canonical time-autodelete runtime not established"
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

        await self._materialize_autodelete(context)

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

        if second.owner_notice is not None and self._owner_notice_authorized(context):
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
