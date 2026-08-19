from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.content_plan_cancellation import (
    ContentPlanCancellationService,
    ContentPlanDeleteResult,
)


class ContentPlanPublicationCancellationService:
    """Publication-native canonical cancellation entrypoint.

    The cancellation core owns persisted execution-mode authority and serializes
    directly on ``Publication``/``ScheduleEntry``. Compatibility ``PostTask`` identity
    is neither required nor used as an authority signal here.
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

        return await ContentPlanCancellationService(
            self.session_factory
        ).delete_canonical_publication(safe_publication_id)
