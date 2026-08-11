from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.content import PostDocument
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendBlockingError,
    CanonicalPublicationDeliveryPostSendContext,
    CanonicalPublicationDeliveryPostSendHook,
)
from app.services.canonical_publication_result_link import (
    CanonicalPublicationResultLinkResolver,
)
from app.services.scheduler_errors import NO_MESSAGE_IDS_ERROR, SAFE_DELIVERY_ERROR
from app.services.telegram_results import normalize_telegram_message_ids


class CanonicalDocumentSender(Protocol):
    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
        disable_notification: bool | None = None,
    ) -> list[int]: ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryExecutionResult:
    publication_id: int
    outcome: Literal["published", "failed", "ineligible", "lease_lost"]
    message_ids: tuple[int, ...] = ()
    result_link: str | None = None
    post_send_hook_failed: bool = False


class CanonicalPublicationDeliveryExecutor:
    """Execute a committed exact-token canonical delivery claim.

    Canonical-only rows use `execute()`, which first obtains a capability claim. Linked
    transport rows may instead use `execute_claim()` after the atomic handoff+claim
    coordinator has committed transport retirement, `sending`, Attempt #1 and the exact
    delivery lease in one transaction.

    Once primary Telegram returns message ids, required post-send semantics (currently
    time-autodelete when enabled) can block terminal success. Such a block leaves the
    durable sending claim for fail-closed recovery and never retries primary delivery.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        sender: CanonicalDocumentSender,
        result_link_resolver: CanonicalPublicationResultLinkResolver | None = None,
        post_send_hook: CanonicalPublicationDeliveryPostSendHook | None = None,
        holder: str = "canonical-publication-delivery",
        lease_seconds: int = 180,
        heartbeat_interval_seconds: float = 45.0,
        allow_time_autodelete: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.sender = sender
        self.result_link_resolver = result_link_resolver
        self.post_send_hook = post_send_hook
        self.holder = str(holder).strip()[:64] or "canonical-publication-delivery"
        self.allow_time_autodelete = bool(allow_time_autodelete)
        try:
            parsed_lease_seconds = int(lease_seconds)
        except (TypeError, ValueError, OverflowError):
            parsed_lease_seconds = 180
        self.lease_seconds = max(30, min(parsed_lease_seconds, 3600))
        try:
            parsed_heartbeat = float(heartbeat_interval_seconds)
        except (TypeError, ValueError, OverflowError):
            parsed_heartbeat = 45.0
        self.heartbeat_interval_seconds = max(
            0.01,
            min(parsed_heartbeat, self.lease_seconds / 2, 120.0),
        )

    async def _claim(
        self,
        publication_id: int,
        *,
        now: datetime | None,
    ) -> CanonicalPublicationDeliveryClaim | None:
        async with self.session_factory() as session:
            return await CanonicalPublicationDeliveryCapabilityClaimService(
                session
            ).claim_supported(
                publication_id=int(publication_id),
                holder=self.holder,
                ttl_seconds=self.lease_seconds,
                now=now,
                allow_time_autodelete=self.allow_time_autodelete,
            )

    async def _heartbeat(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.heartbeat_interval_seconds
                )
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                async with self.session_factory() as session:
                    renewed = await CanonicalPublicationDeliveryClaimService(
                        session
                    ).renew(handle, ttl_seconds=self.lease_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Canonical publication delivery heartbeat failed publication_id={} error_type={}",
                    int(handle.publication_id),
                    type(exc).__name__,
                )
                lost.set()
                return
            if renewed is None:
                lost.set()
                return

    @staticmethod
    async def _stop_heartbeat(
        task: asyncio.Task[None],
        stop: asyncio.Event,
    ) -> None:
        stop.set()
        with suppress(asyncio.CancelledError):
            await task

    async def _finalize_failure(
        self,
        handle: CanonicalPublicationDeliveryLeaseHandle,
        *,
        error: str,
        finished_at: datetime,
    ) -> CanonicalPublicationDeliveryExecutionResult:
        async with self.session_factory() as session:
            result = await CanonicalPublicationDeliveryFinalizer(
                session
            ).complete_failure(handle, error=error, finished_at=finished_at)
        return CanonicalPublicationDeliveryExecutionResult(
            publication_id=int(handle.publication_id),
            outcome="failed" if result.outcome == "failed" else "lease_lost",
        )

    async def _resolve_result_link(
        self,
        *,
        publication_id: int,
        chat_id: int,
        message_ids: list[int],
    ) -> str | None:
        if self.result_link_resolver is None:
            return None
        try:
            return await self.result_link_resolver.resolve(
                chat_id=int(chat_id),
                message_ids=list(message_ids),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Canonical publication result link enrichment failed publication_id={} error_type={}",
                int(publication_id),
                type(exc).__name__,
            )
            return None

    async def _run_post_send_hook(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
    ) -> bool:
        if self.post_send_hook is None:
            return False
        try:
            await self.post_send_hook.execute(context)
            return False
        except asyncio.CancelledError:
            raise
        except CanonicalPublicationDeliveryPostSendBlockingError:
            raise
        except Exception as exc:
            logger.warning(
                "Canonical publication post-send hook failed publication_id={} error_type={}",
                int(context.publication_id),
                type(exc).__name__,
            )
            return True

    @staticmethod
    def _claim_publication_id(
        claim: CanonicalPublicationDeliveryClaim,
    ) -> int | None:
        if not isinstance(claim, CanonicalPublicationDeliveryClaim):
            return None
        try:
            plan_id = int(claim.plan.publication_id)
            lease_id = int(claim.lease.publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if plan_id <= 0 or plan_id != lease_id or int(claim.attempt) != 1:
            return None
        if not str(claim.lease.lease_token):
            return None
        return plan_id

    async def execute_claim(
        self,
        claim: CanonicalPublicationDeliveryClaim,
    ) -> CanonicalPublicationDeliveryExecutionResult:
        """Execute provider work for an already committed exact canonical claim."""

        safe_publication_id = self._claim_publication_id(claim)
        if safe_publication_id is None:
            return CanonicalPublicationDeliveryExecutionResult(0, "ineligible")

        try:
            runtime_options = claim.plan.runtime_options()
        except (TypeError, ValueError):
            return CanonicalPublicationDeliveryExecutionResult(
                safe_publication_id,
                "lease_lost",
            )
        silent_override = (
            runtime_options["silent"] if "silent" in runtime_options else None
        )

        stop, lost = asyncio.Event(), asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(claim.lease, stop=stop, lost=lost),
            name=f"canonical-publication-delivery-heartbeat-{safe_publication_id}",
        )
        try:
            if silent_override is None:
                message_ids = await self.sender.send_document(
                    int(claim.plan.telegram_chat_id),
                    claim.plan.post_document(),
                    asset_channel_id=int(claim.plan.channel_id),
                )
            else:
                message_ids = await self.sender.send_document(
                    int(claim.plan.telegram_chat_id),
                    claim.plan.post_document(),
                    asset_channel_id=int(claim.plan.channel_id),
                    disable_notification=bool(silent_override),
                )
            provider_finished_at = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            await self._stop_heartbeat(heartbeat, stop)
            raise
        except Exception as exc:
            provider_finished_at = datetime.now(timezone.utc)
            logger.warning(
                "Canonical publication delivery provider call failed publication_id={} error_type={}",
                safe_publication_id,
                type(exc).__name__,
            )
            await self._stop_heartbeat(heartbeat, stop)
            if lost.is_set():
                return CanonicalPublicationDeliveryExecutionResult(
                    safe_publication_id, "lease_lost"
                )
            return await self._finalize_failure(
                claim.lease,
                error=SAFE_DELIVERY_ERROR,
                finished_at=provider_finished_at,
            )

        ids = normalize_telegram_message_ids(message_ids)
        if not ids:
            await self._stop_heartbeat(heartbeat, stop)
            if lost.is_set():
                return CanonicalPublicationDeliveryExecutionResult(
                    safe_publication_id, "lease_lost"
                )
            return await self._finalize_failure(
                claim.lease,
                error=NO_MESSAGE_IDS_ERROR,
                finished_at=provider_finished_at,
            )

        try:
            result_link = await self._resolve_result_link(
                publication_id=safe_publication_id,
                chat_id=int(claim.plan.telegram_chat_id),
                message_ids=ids,
            )
            hook_failed = await self._run_post_send_hook(
                CanonicalPublicationDeliveryPostSendContext(
                    publication_id=safe_publication_id,
                    lease=claim.lease,
                    plan=claim.plan,
                    message_ids=tuple(ids),
                    result_link=result_link,
                    primary_finished_at=provider_finished_at,
                )
            )
        except asyncio.CancelledError:
            await self._stop_heartbeat(heartbeat, stop)
            raise
        except CanonicalPublicationDeliveryPostSendBlockingError as exc:
            logger.warning(
                "Canonical publication required post-send semantics blocked terminal success "
                "publication_id={} error_type={}",
                safe_publication_id,
                type(exc).__name__,
            )
            await self._stop_heartbeat(heartbeat, stop)
            return CanonicalPublicationDeliveryExecutionResult(
                safe_publication_id,
                "lease_lost",
                message_ids=tuple(ids),
                result_link=result_link,
                post_send_hook_failed=True,
            )

        await self._stop_heartbeat(heartbeat, stop)
        if lost.is_set():
            return CanonicalPublicationDeliveryExecutionResult(
                safe_publication_id,
                "lease_lost",
                post_send_hook_failed=hook_failed,
            )

        async with self.session_factory() as session:
            finalized = await CanonicalPublicationDeliveryFinalizer(
                session
            ).complete_success(
                claim.lease,
                plan=claim.plan,
                message_ids=ids,
                result_link=result_link,
                finished_at=provider_finished_at,
            )
        if finalized.outcome != "published":
            return CanonicalPublicationDeliveryExecutionResult(
                safe_publication_id,
                "lease_lost",
                post_send_hook_failed=hook_failed,
            )
        return CanonicalPublicationDeliveryExecutionResult(
            safe_publication_id,
            "published",
            message_ids=tuple(ids),
            result_link=result_link,
            post_send_hook_failed=hook_failed,
        )

    async def execute(
        self,
        publication_id: int,
        *,
        now: datetime | None = None,
    ) -> CanonicalPublicationDeliveryExecutionResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_publication_id = 0
        if safe_publication_id <= 0:
            return CanonicalPublicationDeliveryExecutionResult(
                safe_publication_id, "ineligible"
            )

        claim = await self._claim(safe_publication_id, now=now)
        if claim is None:
            return CanonicalPublicationDeliveryExecutionResult(
                safe_publication_id, "ineligible"
            )
        return await self.execute_claim(claim)
