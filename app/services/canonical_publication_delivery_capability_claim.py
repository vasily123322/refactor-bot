from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.domain.publishing.models import PublicationAttempt
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaim,
    CanonicalPublicationDeliveryClaimRequirements,
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlanner,
)
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
    resolve_canonical_publication_delivery_forward_targets,
)
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)


FORWARD_TARGET_SNAPSHOT_META_KEY = "canonical_forward_targets"


class CanonicalPublicationDeliveryCapabilityClaimService(
    CanonicalPublicationDeliveryClaimService
):
    """Concrete claim profile for explicitly enabled canonical delivery capabilities.

    Time/views delete and repeat authority are independently gated by concrete runtime
    dependencies. Repeat remains default-off and is admitted only when a successfully
    started canonical continuation worker can guarantee successor recovery after terminal
    canonical delivery. Overdue/boot semantics remain separate legacy-owned authority.
    """

    async def claim_supported(
        self,
        *,
        publication_id: int,
        holder: str,
        ttl_seconds: int,
        now: datetime | None = None,
        allow_time_autodelete: bool = False,
        allow_views_autodelete: bool = False,
        allow_repeat: bool = False,
    ) -> CanonicalPublicationDeliveryClaim | None:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if safe_publication_id <= 0:
            return None

        current = now or datetime.now(timezone.utc)
        locked = await self._lock_delivery_rows(safe_publication_id)
        if locked is None:
            await self.session.rollback()
            return None

        plan = await CanonicalPublicationDeliveryPlanner(self.session).plan(
            safe_publication_id,
            at=current,
        )
        if plan is None:
            await self.session.rollback()
            return None
        try:
            options = plan.runtime_options()
        except (TypeError, ValueError):
            await self.session.rollback()
            return None
        capability = parse_canonical_publication_delivery_runtime_capability(options)
        if capability is None:
            await self.session.rollback()
            return None
        if capability.time_autodelete_requested and not allow_time_autodelete:
            await self.session.rollback()
            return None
        if capability.views_autodelete_requested and not allow_views_autodelete:
            await self.session.rollback()
            return None

        targets = await resolve_canonical_publication_delivery_forward_targets(
            self.session,
            capability,
            lock=True,
        )
        if targets is None:
            await self.session.rollback()
            return None
        target_snapshot = [
            {
                "channel_id": int(target.channel_id),
                "telegram_chat_id": int(target.telegram_chat_id),
            }
            for target in targets
        ]

        # Stage indexed views intent before the lower claim's single authority commit.
        await PublicationAutodeleteViewStateService(self.session).sync_intent(
            publication_id=safe_publication_id,
            threshold=capability.views_autodelete_threshold,
            now=current,
        )

        claim = await super().claim(
            publication_id=safe_publication_id,
            holder=holder,
            ttl_seconds=ttl_seconds,
            now=current,
            requirements=CanonicalPublicationDeliveryClaimRequirements(
                require_empty_runtime_options=False,
                require_nonrepeat=not bool(allow_repeat),
                require_transport_retired=True,
            ),
        )
        if claim is None:
            return None

        # Persist all resolved forward destinations before any provider call. Failure
        # after authority transition leaves `sending + lease` for recovery.
        try:
            attempt = (
                await self.session.execute(
                    select(PublicationAttempt).where(
                        PublicationAttempt.publication_id == safe_publication_id,
                        PublicationAttempt.attempt == 1,
                        PublicationAttempt.status == "sending",
                        PublicationAttempt.finished_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if attempt is None:
                await self.session.rollback()
                return None
            attempt_meta = dict(attempt.meta or {})
            if attempt_meta.get("canonical_delivery") is not True:
                await self.session.rollback()
                return None
            attempt_meta[FORWARD_TARGET_SNAPSHOT_META_KEY] = target_snapshot
            attempt.meta = attempt_meta
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        renew_at = current if now is not None else None
        renewed = await super().renew(
            claim.lease,
            ttl_seconds=ttl_seconds,
            now=renew_at,
        )
        if renewed is None:
            return None

        return CanonicalPublicationDeliveryClaim(
            plan=claim.plan,
            lease=renewed,
            attempt=claim.attempt,
        )
