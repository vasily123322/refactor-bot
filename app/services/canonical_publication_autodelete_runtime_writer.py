from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.canonical_publication_autodelete_runtime_planner import (
    CanonicalPublicationAutodeleteRuntimePlanner,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


@dataclass(frozen=True, slots=True)
class CanonicalPublicationAutodeleteRuntimeWriteResult:
    publication_id: int
    outcome: Literal["created", "existing", "ineligible", "conflict"]


class CanonicalPublicationAutodeleteRuntimeWriter:
    """Atomically materialize one proven canonical autodelete runtime token.

    Generated runtime belongs only to ``Publication.meta``. Queue-time intent remains
    unchanged on Publication/Schedule, and no transport row or Telegram side effect is
    created here.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lock_proof_rows(self, publication_id: int) -> Publication | None:
        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.id == int(publication_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None or publication.schedule_entry_id is None:
            return None

        schedule = (
            await self.session.execute(
                select(ScheduleEntry)
                .where(ScheduleEntry.id == int(publication.schedule_entry_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if schedule is None:
            return None

        attempt_number = int(publication.attempt_count or 0)
        if attempt_number <= 0:
            return None
        attempt = (
            await self.session.execute(
                select(PublicationAttempt)
                .where(
                    PublicationAttempt.publication_id == int(publication.id),
                    PublicationAttempt.attempt == attempt_number,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if attempt is None:
            return None
        return publication

    async def materialize(
        self,
        publication_id: int,
    ) -> CanonicalPublicationAutodeleteRuntimeWriteResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            safe_publication_id = 0
        if safe_publication_id <= 0:
            return CanonicalPublicationAutodeleteRuntimeWriteResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )

        planner = CanonicalPublicationAutodeleteRuntimePlanner(self.session)
        initial = await planner.plan(safe_publication_id)
        if initial is None:
            await self.session.rollback()
            return CanonicalPublicationAutodeleteRuntimeWriteResult(
                publication_id=safe_publication_id,
                outcome="ineligible",
            )
        if initial.existing:
            await self.session.rollback()
            return CanonicalPublicationAutodeleteRuntimeWriteResult(
                publication_id=safe_publication_id,
                outcome="existing",
            )

        try:
            publication = await self._lock_proof_rows(safe_publication_id)
            if publication is None:
                await self.session.rollback()
                return CanonicalPublicationAutodeleteRuntimeWriteResult(
                    publication_id=safe_publication_id,
                    outcome="conflict",
                )

            proven = await planner.plan(safe_publication_id)
            if proven is None:
                await self.session.rollback()
                return CanonicalPublicationAutodeleteRuntimeWriteResult(
                    publication_id=safe_publication_id,
                    outcome="conflict",
                )
            if proven.existing:
                await self.session.rollback()
                return CanonicalPublicationAutodeleteRuntimeWriteResult(
                    publication_id=safe_publication_id,
                    outcome="existing",
                )
            if proven.state != initial.state:
                await self.session.rollback()
                return CanonicalPublicationAutodeleteRuntimeWriteResult(
                    publication_id=safe_publication_id,
                    outcome="conflict",
                )

            original_meta = dict(publication.meta or {})
            if AUTODELETE_RUNTIME_META_KEY in original_meta:
                await self.session.rollback()
                return CanonicalPublicationAutodeleteRuntimeWriteResult(
                    publication_id=safe_publication_id,
                    outcome="conflict",
                )
            publication.meta = {
                **deepcopy(original_meta),
                AUTODELETE_RUNTIME_META_KEY: deepcopy(proven.state),
            }
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return CanonicalPublicationAutodeleteRuntimeWriteResult(
            publication_id=safe_publication_id,
            outcome="created",
        )
