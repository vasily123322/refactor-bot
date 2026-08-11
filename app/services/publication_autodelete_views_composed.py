from __future__ import annotations

from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_views_lifecycle_authority import (
    CanonicalRepeatViewsLifecycleAuthorityService,
)
from app.services.publication_autodelete_views import _safe_nonrepeat
from app.services.publication_autodelete_views_destructive import (
    PublicationAutodeleteViewsDestructiveService,
)


class PublicationAutodeleteViewsComposedDestructiveService(
    PublicationAutodeleteViewsDestructiveService
):
    """Narrow composition adapter over the durable reserve-before-DELETE boundary.

    Plain repeat+views keeps the established lifecycle proof. Views+pin requires one
    additional explicit fact and feeds it only into the provider-free lifecycle proof;
    every destructive reservation/fingerprint/lease/no-replay rule remains inherited
    unchanged from the #290 boundary.

    Production does not use this adapter until a later startup-activation stage.
    """

    def __init__(
        self,
        *args,
        allow_repeat_views_pin: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.allow_repeat_views_pin = bool(allow_repeat_views_pin)

    async def _lifecycle_allowed(
        self,
        publication: Publication,
        schedule: ScheduleEntry,
        *,
        telegram_message_ids: tuple[int, ...],
        threshold: int,
        report_enabled: bool,
    ) -> bool:
        if _safe_nonrepeat(schedule):
            return True
        if not self.allow_repeat_views:
            return False

        proof = await CanonicalRepeatViewsLifecycleAuthorityService(
            self.session
        ).lock_and_prove(
            int(publication.id),
            allow_pin=self.allow_repeat_views_pin,
        )
        if proof is None:
            return False
        return (
            proof.publication_id == int(publication.id)
            and proof.schedule_entry_id == int(schedule.id)
            and proof.threshold == int(threshold)
            and proof.autodelete_report is bool(report_enabled)
            and proof.telegram_message_ids == telegram_message_ids
        )