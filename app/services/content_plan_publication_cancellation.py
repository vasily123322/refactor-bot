from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.publishing.models import Publication
from app.services.content_plan_cancellation import (
    ContentPlanCancellationService,
    ContentPlanDeleteResult,
)


class ContentPlanPublicationCancellationService:
    """Canonical UI adapter for the existing cancellation serialization boundary.

    Content-plan callbacks can identify queued work by ``Publication.id`` while the
    current atomic cancellation core still fences and retires its compatibility
    ``PostTask``. The transport identity is resolved here and is not exposed back into
    callback data.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def delete(self, publication_id: int) -> ContentPlanDeleteResult:
        try:
            safe_publication_id = int(publication_id)
        except (TypeError, ValueError, OverflowError):
            return ContentPlanDeleteResult(
                outcome="cannot_cancel",
                reason="invalid_publication_id",
            )
        if safe_publication_id <= 0:
            return ContentPlanDeleteResult(
                outcome="cannot_cancel",
                reason="invalid_publication_id",
            )

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Publication.status, Publication.legacy_post_task_id).where(
                        Publication.id == safe_publication_id
                    )
                )
            ).one_or_none()
        if row is None:
            return ContentPlanDeleteResult(outcome="already_absent")

        status, legacy_post_task_id = row
        if str(status) != "queued":
            return ContentPlanDeleteResult(
                outcome="cannot_cancel",
                reason=f"publication_{status or 'unknown'}",
            )
        if legacy_post_task_id is None:
            return ContentPlanDeleteResult(
                outcome="cannot_cancel",
                reason="compatibility_transport_absent",
            )
        try:
            task_id = int(legacy_post_task_id)
        except (TypeError, ValueError, OverflowError):
            return ContentPlanDeleteResult(
                outcome="cannot_cancel",
                reason="invalid_compatibility_transport",
            )
        if task_id <= 0:
            return ContentPlanDeleteResult(
                outcome="cannot_cancel",
                reason="invalid_compatibility_transport",
            )

        return await ContentPlanCancellationService(self.session_factory).delete(task_id)
