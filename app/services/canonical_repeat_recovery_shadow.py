from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Literal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.services.canonical_repeat_recovery_reservation import (
    CanonicalRepeatRecoveryReservationService,
)
from app.services.canonical_repeat_recovery_verifier import (
    CanonicalRepeatRecoveryVerifier,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge


@dataclass(frozen=True, slots=True)
class CanonicalRepeatRecoveryShadowResult:
    legacy_recovered: bool
    source_publication_id: int | None = None
    reservation_outcome: Literal[
        "reserved",
        "already_reserved",
        "existing_successor",
        "ineligible",
        "conflict",
        "failed",
    ] | None = None
    verification_outcome: Literal[
        "matched",
        "pending",
        "ineligible",
        "conflict",
        "failed",
    ] | None = None


class CanonicalRepeatRecoveryShadowCoordinator:
    """Observe legacy overdue recovery without becoming its execution dependency.

    Reservation, canonical source projection and verification are best-effort shadow
    operations. The supplied legacy callback is always invoked exactly once unless it
    raises/cancels itself. Canonical failures are logged and returned as shadow outcomes
    but never replace the legacy recovery decision.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _source_publication_id(self, post: PostTask) -> int | None:
        bridge = LegacyPublicationBridge(self.session)
        publication = await bridge.reconcile_task(post)
        if publication is None:
            publication = await mirror_legacy_post_task(self.session, post)
        return int(publication.id) if publication is not None else None

    async def _project_recovered_source(self, post_id: int) -> None:
        """Mirror the terminal source state before shadow verification.

        PublicationScheduler normally performs this projection after its base scheduler
        returns. A shadow coordinator wrapped directly around the overdue helper runs
        earlier than that outer boundary, so project the same state here. The bridge is
        idempotent and the later normal projection remains safe.
        """

        current = await self.session.get(PostTask, int(post_id))
        if current is None:
            return
        await LegacyPublicationBridge(self.session).reconcile_task(current)

    async def run(
        self,
        *,
        post: PostTask,
        after: datetime,
        legacy_recover: Callable[[], Awaitable[bool]],
    ) -> CanonicalRepeatRecoveryShadowResult:
        source_publication_id: int | None = None
        reservation_outcome: str | None = None

        try:
            source_publication_id = await self._source_publication_id(post)
            if source_publication_id is not None:
                reservation = await CanonicalRepeatRecoveryReservationService(
                    self.session
                ).reserve_recovery(
                    source_publication_id,
                    after=after,
                )
                reservation_outcome = reservation.outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reservation_outcome = "failed"
            logger.warning(
                "Canonical repeat recovery shadow reservation failed post_id={} type={}",
                int(post.id),
                type(exc).__name__,
            )

        # The legacy callback remains authoritative and is never skipped because of
        # canonical shadow state or failure.
        legacy_recovered = await legacy_recover()

        verification_outcome: str | None = None
        if source_publication_id is not None and legacy_recovered:
            try:
                # Match the full PublicationScheduler runtime boundary: legacy recovery
                # has already committed the skipped PostTask + child, while the normal
                # outer scheduler projects the source immediately afterwards. Doing the
                # same projection here lets shadow verification observe production state
                # without changing the legacy scheduling decision.
                await self._project_recovered_source(int(post.id))
                verification = await CanonicalRepeatRecoveryVerifier(
                    self.session
                ).verify(source_publication_id)
                verification_outcome = verification.outcome
                if verification.outcome == "matched":
                    logger.debug(
                        "Canonical repeat recovery shadow matched source_publication_id={} "
                        "post_id={} successor_publication_id={}",
                        source_publication_id,
                        int(post.id),
                        verification.successor_publication_id,
                    )
                else:
                    logger.warning(
                        "Canonical repeat recovery shadow mismatch source_publication_id={} "
                        "post_id={} outcome={}",
                        source_publication_id,
                        int(post.id),
                        verification.outcome,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                verification_outcome = "failed"
                logger.warning(
                    "Canonical repeat recovery shadow verification failed "
                    "source_publication_id={} post_id={} type={}",
                    source_publication_id,
                    int(post.id),
                    type(exc).__name__,
                )

        return CanonicalRepeatRecoveryShadowResult(
            legacy_recovered=bool(legacy_recovered),
            source_publication_id=source_publication_id,
            reservation_outcome=reservation_outcome,  # type: ignore[arg-type]
            verification_outcome=verification_outcome,  # type: ignore[arg-type]
        )
