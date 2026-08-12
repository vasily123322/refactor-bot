from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.canonical_repeat_time_pin_forward_autodelete import (
    CanonicalRepeatTimePinForwardAutodeleteService,
)
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle
from app.workers.publication_autodelete import PublicationAutodeleteWorker


class CanonicalRepeatTimePinForwardAutodeleteWorker(PublicationAutodeleteWorker):
    """Dedicated started-only dependency for the combined time composition."""

    def __init__(
        self,
        *,
        provider,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        interval_seconds: int = 60,
        batch_size: int = 25,
        lease_ttl_seconds: int = 180,
        allow_repeat_time_pin_forward: bool = False,
    ) -> None:
        super().__init__(
            provider=provider,
            session_factory=session_factory,
            interval_seconds=interval_seconds,
            batch_size=batch_size,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        self.allow_repeat_time_pin_forward = bool(allow_repeat_time_pin_forward)
        self._started = False

    @property
    def repeat_time_pin_forward_available(self) -> bool:
        return bool(self._started and self.allow_repeat_time_pin_forward)

    async def start(self) -> None:
        await self._loop.start()
        self._started = True

    async def stop(self) -> None:
        try:
            await self._loop.stop()
        finally:
            self._started = False

    async def _delete_with_operation_session(
        self,
        operation_session: AsyncSession,
        *,
        publication_id: int,
        handle: PublicationAutodeleteLeaseHandle,
    ):
        return await CanonicalRepeatTimePinForwardAutodeleteService(
            operation_session,
            provider=self.provider,
            allow_repeat_time_pin_forward=self.allow_repeat_time_pin_forward,
            allow_report=True,
        ).delete_if_due(
            int(publication_id),
            lease=handle,
        )
