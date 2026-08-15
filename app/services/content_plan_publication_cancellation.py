from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.content_plan_cancellation import (
    ContentPlanCancellationService,
    ContentPlanDeleteResult,
)


class ContentPlanPublicationCancellationService:
    """Publication-id facade for canonical content-plan cancellation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def delete(self, publication_id: int) -> ContentPlanDeleteResult:
        return await ContentPlanCancellationService(
            self.session_factory
        ).delete_publication(publication_id)
