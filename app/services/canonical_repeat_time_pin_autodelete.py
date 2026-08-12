from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.canonical_repeat_time_autodelete import (
    CanonicalRepeatTimeAutodeleteService,
)
from app.services.canonical_repeat_time_pin_lifecycle_authority import (
    CanonicalRepeatTimePinLifecycleAuthorityService,
)
from app.services.publication_autodelete import TelegramDeleteProvider


class CanonicalRepeatTimePinAutodeleteService(CanonicalRepeatTimeAutodeleteService):
    """Admit exact repeat+time+pin into the same #281 destructive owner.

    Only the provider-free terminal lifecycle proof differs from plain repeat+time.
    Candidate fingerprinting, exact autodelete lease, committed per-message reservation,
    provider invocation, ambiguity classification and terminal action finalization remain
    inherited from the single established #281-backed implementation.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: TelegramDeleteProvider,
        allow_repeat_time_pin: bool = False,
        allow_report: bool = False,
    ) -> None:
        self.allow_repeat_time_pin = bool(allow_repeat_time_pin)
        super().__init__(
            session,
            provider=provider,
            allow_repeat_time=self.allow_repeat_time_pin,
            allow_report=allow_report,
        )

    async def _prove_lifecycle(self, publication_id: int):
        if not self.allow_repeat_time_pin:
            return None
        return await CanonicalRepeatTimePinLifecycleAuthorityService(
            self.session
        ).lock_and_prove(
            publication_id,
            allow_time_pin=True,
        )
