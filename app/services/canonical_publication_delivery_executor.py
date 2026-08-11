from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.content import PostDocument
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
    CanonicalPublicationDeliveryClaimRequirements,
    CanonicalPublicationDeliveryClaimService,
    CanonicalPublicationDeliveryLeaseHandle,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
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
    ) -> list[int]: ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryExecutionResult:
    publication_id: int
    outcome: Literal["published", "failed", "ineligible", "lease_lost"]
    message_ids: tuple[int, ...] = ()


_EXECUTOR_CLAIM_REQUIREMENTS = CanonicalPublicationDeliveryClaimRequirements(
    require_empty_runtime_options=True,
    require_nonrepeat=True,
)


class CanonicalPublicationDeliveryExecutor:
    """Execute one currently supported canonical Publication delivery slice.

    Claim/finalize operations use short independent DB sessions. Telegram delivery is
    performed outside a DB transaction while a background heartbeat renews the typed
    Publication lease from separate sessions. If ownership is lost, this worker never
    commits a stale outcome even when the provider call returned successfully.

    The current transport adapter supports only non-repeat Publications with no separate
    runtime options. Those capability restrictions are enforced inside the atomic claim
    transaction, before Publication enters ``sending``. Later migration stages can widen
    the requirements only when each runtime behavior has a canonical implementation.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        sender: CanonicalDocumentSender,
        holder: str = "canonical-publication-delivery",
        lease_seconds: int = 180,
        heartbeat_interval_seconds: float = 45.0,
    ) -> None:
        self.session_factory = session_factory
        self.sender = sender
        self.holder = str(holder).strip()[:64] or "canonical-publication-delivery"
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
            return await CanonicalPublicationDeliveryClaimService(session).claim(
                publication_id=int(publication_id),
                holder=self.holder,
                ttl_seconds=self.lease_seconds,
                now=now,
                requirements=_EXECUTOR_CLAIM_REQUIREMENTS,
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
                    stop.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

            try:
                async with self.session_factory() as session:
                    renewed = await CanonicalPublicationDeliveryClaimService(session).renew(
                        handle,
                        ttl_seconds=self.lease_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Canonical publication delivery heartbeat failed publication_id={} "
                    "error_type={}",
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
            ).complete_failure(
                handle,
                error=error,
                finished_at=finished_at,
            )
        if result.outcome == "failed":
            return CanonicalPublicationDeliveryExecutionResult(
                publication_id=int(handle.publication_id),
                outcome="failed",
            )
        return CanonicalPublicationDeliveryExecutionResult(
            publication_id=int(handle.publication_id),
            outcome="lease_lost",
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
                publication_id=safe_publication_id,
                outcome="ineligible",
            )

        claim = await self._claim(safe_publication_id, now=now)
        if claim is None:
            return CanonicalPublicationDeliveryExecutionResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )

        stop = asyncio.Event()
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(claim.lease, stop=stop, lost=lost),
            name=f"canonical-publication-delivery-heartbeat-{safe_publication_id}",
        )

        try:
            message_ids = await self.sender.send_document(
                int(claim.plan.telegram_chat_id),
                claim.plan.post_document(),
                asset_channel_id=int(claim.plan.channel_id),
            )
            provider_finished_at = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            await self._stop_heartbeat(heartbeat, stop)
            raise
        except Exception as exc:
            provider_finished_at = datetime.now(timezone.utc)
            logger.warning(
                "Canonical publication delivery provider call failed publication_id={} "
                "error_type={}",
                safe_publication_id,
                type(exc).__name__,
            )
            await self._stop_heartbeat(heartbeat, stop)
            if lost.is_set():
                return CanonicalPublicationDeliveryExecutionResult(
                    publication_id=safe_publication_id,
                    outcome="lease_lost",
                )
            return await self._finalize_failure(
                claim.lease,
                error=SAFE_DELIVERY_ERROR,
                finished_at=provider_finished_at,
            )

        await self._stop_heartbeat(heartbeat, stop)
        if lost.is_set():
            return CanonicalPublicationDeliveryExecutionResult(
                publication_id=safe_publication_id,
                outcome="lease_lost",
            )

        ids = normalize_telegram_message_ids(message_ids)
        if not ids:
            return await self._finalize_failure(
                claim.lease,
                error=NO_MESSAGE_IDS_ERROR,
                finished_at=provider_finished_at,
            )

        async with self.session_factory() as session:
            finalized = await CanonicalPublicationDeliveryFinalizer(
                session
            ).complete_success(
                claim.lease,
                message_ids=ids,
                finished_at=provider_finished_at,
            )
        if finalized.outcome != "published":
            return CanonicalPublicationDeliveryExecutionResult(
                publication_id=safe_publication_id,
                outcome="lease_lost",
            )
        return CanonicalPublicationDeliveryExecutionResult(
            publication_id=safe_publication_id,
            outcome="published",
            message_ids=tuple(ids),
        )
