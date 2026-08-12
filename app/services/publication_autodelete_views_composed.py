from __future__ import annotations

from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_views_lifecycle_authority import (
    CanonicalRepeatViewsLifecycleAuthorityService,
)
from app.services.publication_autodelete_views import (
    PublicationAutodeleteViewsService,
    _safe_nonrepeat,
)


class PublicationAutodeleteViewsComposedService(PublicationAutodeleteViewsService):
    """Narrow views effect compositions over the current #300 destructive boundary.

    The parent service remains the sole owner of candidate fingerprinting, exact live
    autodelete lease verification, durable per-message reservation, immutable provider
    target capture and reserved/unknown no-replay semantics. This adapter changes only the
    provider-free repeat lifecycle proof.

    Views+pin and views+ordered-forward have independent default-off facts. Supplying both
    independent facts never grants views+pin+forward authority because the lifecycle proof
    keeps that combined profile closed until its own later explicit composition stage.
    """

    def __init__(
        self,
        *args,
        allow_repeat_views_pin: bool = False,
        allow_repeat_views_forward: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.allow_repeat_views_pin = bool(allow_repeat_views_pin)
        self.allow_repeat_views_forward = bool(allow_repeat_views_forward)

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
            allow_forward=self.allow_repeat_views_forward,
        )
        if proof is None:
            return False
        return (
            proof.publication_id == int(publication.id)
            and proof.schedule_entry_id == int(schedule.id)
            and proof.threshold == int(threshold)
            and proof.autodelete_report == bool(report_enabled)
            and proof.telegram_message_ids == telegram_message_ids
        )
