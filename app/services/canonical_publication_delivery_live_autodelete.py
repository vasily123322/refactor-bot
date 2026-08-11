from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlanner,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.canonical_publication_delivery_runtime_capability import (
    parse_canonical_publication_delivery_runtime_capability,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryLiveAutodeleteResult:
    publication_id: int
    outcome: Literal["not_requested", "created", "existing", "ineligible", "conflict"]


class CanonicalPublicationDeliveryLiveAutodeleteWriter:
    """Materialize requested time-autodelete while primary delivery ownership is live.

    The generated runtime token is written after the primary Telegram send but before
    terminal publication success. This preserves the historical scheduling boundary while
    making timer survival a durable prerequisite for canonical terminal success.

    No Telegram method is called here. A crash after this write is safe: the Publication
    remains `sending`, so the canonical autodelete worker cannot act until recovery or an
    exact live owner later establishes terminal `published` state.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_proof_rows(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        *,
        at: datetime,
    ) -> bool:
        try:
            publication_id = int(context.publication_id)
            schedule_entry_id = int(context.plan.schedule_entry_id)
            content_item_id = int(context.plan.content_item_id)
            content_revision = int(context.plan.content_revision)
            channel_id = int(context.plan.channel_id)
        except (TypeError, ValueError, OverflowError):
            return False

        lease = (
            await self.session.execute(
                select(PublicationDeliveryLease)
                .where(
                    PublicationDeliveryLease.publication_id == publication_id,
                    PublicationDeliveryLease.lease_token == str(context.lease.lease_token),
                    PublicationDeliveryLease.expires_at > at,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            return False

        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.id == publication_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None:
            return False

        schedule = (
            await self.session.execute(
                select(ScheduleEntry)
                .where(ScheduleEntry.id == schedule_entry_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        item = (
            await self.session.execute(
                select(ContentItem)
                .where(ContentItem.id == content_item_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        revision = (
            await self.session.execute(
                select(ContentRevision)
                .where(
                    ContentRevision.content_item_id == content_item_id,
                    ContentRevision.revision == content_revision,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        channel = (
            await self.session.execute(
                select(Channel)
                .where(Channel.id == channel_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        attempt = (
            await self.session.execute(
                select(PublicationAttempt)
                .where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return all(
            row is not None
            for row in (schedule, item, revision, channel, attempt)
        )

    async def materialize(
        self,
        context: CanonicalPublicationDeliveryPostSendContext,
        *,
        at: datetime | None = None,
    ) -> CanonicalPublicationDeliveryLiveAutodeleteResult:
        try:
            publication_id = int(context.publication_id)
            runtime_options = context.plan.runtime_options()
        except (TypeError, ValueError, OverflowError):
            return CanonicalPublicationDeliveryLiveAutodeleteResult(
                publication_id=0,
                outcome="ineligible",
            )
        capability = parse_canonical_publication_delivery_runtime_capability(
            runtime_options
        )
        if capability is None:
            return CanonicalPublicationDeliveryLiveAutodeleteResult(
                publication_id=publication_id,
                outcome="ineligible",
            )
        seconds = capability.time_autodelete_seconds
        if seconds is None:
            return CanonicalPublicationDeliveryLiveAutodeleteResult(
                publication_id=publication_id,
                outcome="not_requested",
            )

        current = _utc(at)
        authorized = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
            self.session
        ).plan(context, at=current)
        if authorized is None:
            await self.session.rollback()
            return CanonicalPublicationDeliveryLiveAutodeleteResult(
                publication_id=publication_id,
                outcome="ineligible",
            )

        due_at = _utc(context.primary_finished_at) + timedelta(seconds=int(seconds))
        desired = {
            "deleted": False,
            "effective_seconds": int(seconds),
            "scheduled_at": due_at.isoformat(),
        }

        try:
            if not await self._lock_proof_rows(context, at=current):
                await self.session.rollback()
                return CanonicalPublicationDeliveryLiveAutodeleteResult(
                    publication_id=publication_id,
                    outcome="conflict",
                )

            # Re-run the complete live intent/lease proof while every mutable row used by
            # it is locked. This catches source, document, runtime, repeat or lifecycle
            # drift between the initial proof and the generated-runtime commit.
            reproved = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                self.session
            ).plan(context, at=current)
            if reproved is None:
                await self.session.rollback()
                return CanonicalPublicationDeliveryLiveAutodeleteResult(
                    publication_id=publication_id,
                    outcome="conflict",
                )

            publication = await self.session.get(Publication, publication_id)
            if publication is None or not isinstance(publication.meta, Mapping):
                await self.session.rollback()
                return CanonicalPublicationDeliveryLiveAutodeleteResult(
                    publication_id=publication_id,
                    outcome="conflict",
                )
            meta = dict(publication.meta)
            existing_raw = meta.get(AUTODELETE_RUNTIME_META_KEY)
            if existing_raw is not None:
                if not isinstance(existing_raw, Mapping) or dict(existing_raw) != desired:
                    await self.session.rollback()
                    return CanonicalPublicationDeliveryLiveAutodeleteResult(
                        publication_id=publication_id,
                        outcome="conflict",
                    )
                await self.session.rollback()
                return CanonicalPublicationDeliveryLiveAutodeleteResult(
                    publication_id=publication_id,
                    outcome="existing",
                )

            publication.meta = {
                **deepcopy(meta),
                AUTODELETE_RUNTIME_META_KEY: deepcopy(desired),
            }
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return CanonicalPublicationDeliveryLiveAutodeleteResult(
            publication_id=publication_id,
            outcome="created",
        )
