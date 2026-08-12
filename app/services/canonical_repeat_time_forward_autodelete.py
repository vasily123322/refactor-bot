from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_repeat_time_autodelete import (
    CanonicalRepeatTimeAutodeleteService,
)
from app.services.canonical_repeat_time_forward_lifecycle_authority import (
    CanonicalRepeatTimeForwardLifecycleAuthorityService,
)
from app.services.publication_autodelete import TelegramDeleteProvider


class CanonicalRepeatTimeForwardAutodeleteService(CanonicalRepeatTimeAutodeleteService):
    """Admit exact ordered repeat+time+forward into the same #281 DELETE owner.

    Only terminal provider-free lifecycle admission differs. Exact lease, candidate
    fingerprint, per-message reservation, provider-after-commit boundary, ambiguity
    barrier and finalization all remain inherited from the established destructive path.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: TelegramDeleteProvider,
        allow_repeat_time_forward: bool = False,
        allow_report: bool = False,
    ) -> None:
        self.allow_repeat_time_forward = bool(allow_repeat_time_forward)
        super().__init__(
            session,
            provider=provider,
            allow_repeat_time=self.allow_repeat_time_forward,
            allow_report=allow_report,
        )

    async def _prove_lifecycle(self, publication_id: int):
        if not self.allow_repeat_time_forward:
            return None
        return await CanonicalRepeatTimeForwardLifecycleAuthorityService(
            self.session
        ).lock_and_prove(
            publication_id,
            allow_time_forward=True,
        )
