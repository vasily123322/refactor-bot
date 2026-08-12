from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_repeat_time_autodelete import CanonicalRepeatTimeAutodeleteService
from app.services.canonical_repeat_time_pin_forward_lifecycle_authority import (
    CanonicalRepeatTimePinForwardLifecycleAuthorityService,
)
from app.services.publication_autodelete import TelegramDeleteProvider


class CanonicalRepeatTimePinForwardAutodeleteService(CanonicalRepeatTimeAutodeleteService):
    """Admit exact repeat+time+pin+forward into the same #281 DELETE owner.

    Only the provider-free terminal lifecycle proof differs. Exact lease, destructive
    fingerprint, committed reservation, provider-after-commit boundary, ambiguity barrier
    and finalization remain inherited from the established #281-backed implementation.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: TelegramDeleteProvider,
        allow_repeat_time_pin_forward: bool = False,
        allow_report: bool = False,
    ) -> None:
        self.allow_repeat_time_pin_forward = bool(allow_repeat_time_pin_forward)
        super().__init__(
            session,
            provider=provider,
            allow_repeat_time=self.allow_repeat_time_pin_forward,
            allow_report=allow_report,
        )

    async def _prove_lifecycle(self, publication_id: int):
        if not self.allow_repeat_time_pin_forward:
            return None
        return await CanonicalRepeatTimePinForwardLifecycleAuthorityService(
            self.session
        ).lock_and_prove(
            publication_id,
            allow_time_pin_forward=True,
        )
