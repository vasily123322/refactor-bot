from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.models import Channel
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.canonical_repeat_time_lifecycle_authority import (
    CanonicalRepeatTimeLifecycleAuthorityService,
)
from app.services.publication_autodelete import (
    PublicationAutodeleteResult,
    PublicationAutodeleteService,
    TelegramDeleteProvider,
    _Candidate,
    _fingerprint,
    _safe_mapping,
    _utc,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


class CanonicalRepeatTimeAutodeleteService(PublicationAutodeleteService):
    """Admit plain repeat+time into the existing durable DELETE authority path.

    This adapter does not create a second destructive executor. The inherited #281
    implementation remains the only provider-call path: exact publication lease,
    deterministic per-message fingerprint, committed reservation, provider outside the
    transaction, and exact reservation finalization.

    The adapter only widens candidate admission for one proven terminal canonical repeat
    occurrence. Its protected lifecycle seam is re-run before every preflight/reserve/
    finalize candidate load, allowing narrower composition adapters to replace only the
    provider-free proof while retaining this exact destructive implementation.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: TelegramDeleteProvider,
        allow_repeat_time: bool = False,
        allow_report: bool = False,
    ) -> None:
        super().__init__(session, provider=provider, allow_report=allow_report)
        self.allow_repeat_time = bool(allow_repeat_time)

    async def _prove_lifecycle(self, publication_id: int):
        return await CanonicalRepeatTimeLifecycleAuthorityService(
            self.session
        ).lock_and_prove(
            publication_id,
            allow_time=True,
        )

    def _candidate_from_row(
        self,
        *,
        publication: Publication,
        schedule: ScheduleEntry,
        item: ContentItem,
        channel: Channel,
        now: datetime,
    ) -> tuple[_Candidate | None, PublicationAutodeleteResult]:
        # Reuse every #281 time-candidate check except its historical non-repeat gate.
        # A lightweight proxy changes only that one input; the returned fingerprint is
        # immediately rebuilt with the real repeat rule so repeat identity remains part
        # of immutable destructive authority.
        nonrepeat_schedule = SimpleNamespace(
            id=schedule.id,
            scheduled_at=schedule.scheduled_at,
            timezone=schedule.timezone,
            repeat_rule=None,
        )
        candidate, result = super()._candidate_from_row(
            publication=publication,
            schedule=nonrepeat_schedule,  # type: ignore[arg-type]
            item=item,
            channel=channel,
            now=now,
        )
        if candidate is None:
            return None, result

        raw_repeat = schedule.repeat_rule
        if not isinstance(raw_repeat, dict):
            try:
                repeat_rule = dict(raw_repeat or {})
            except (TypeError, ValueError):
                return None, PublicationAutodeleteResult(
                    int(publication.id),
                    "ineligible",
                )
        else:
            repeat_rule = dict(raw_repeat)

        meta = _safe_mapping(publication.meta)
        runtime = _safe_mapping(meta.get(AUTODELETE_RUNTIME_META_KEY))
        runtime_options = _safe_mapping(meta.get("runtime_options"))
        authority_fingerprint = _fingerprint(
            {
                "version": 1,
                "publication_id": int(candidate.publication_id),
                "channel_id": int(candidate.channel_id),
                "telegram_chat_id": int(candidate.tg_chat_id),
                "content_item_id": int(candidate.content_item_id),
                "content_revision": int(candidate.content_revision),
                "schedule_entry_id": int(candidate.schedule_entry_id),
                "schedule_scheduled_at": _utc(schedule.scheduled_at).isoformat(),
                "schedule_timezone": schedule.timezone,
                "runtime_scheduled_at": str(candidate.runtime_scheduled_at),
                "runtime": runtime,
                "runtime_options": runtime_options,
                "repeat_rule": repeat_rule,
                "telegram_message_ids": list(candidate.telegram_message_ids),
                "result_link": candidate.result_link,
                "report_enabled": bool(candidate.report_enabled),
            }
        )
        if authority_fingerprint is None:
            return None, PublicationAutodeleteResult(
                int(publication.id),
                "ineligible",
            )
        return replace(
            candidate,
            authority_fingerprint=authority_fingerprint,
        ), result

    async def _load_candidate(
        self,
        publication_id: int,
        *,
        now: datetime,
        lock: bool,
    ) -> tuple[_Candidate | None, PublicationAutodeleteResult]:
        if not self.allow_repeat_time:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                int(publication_id),
                "ineligible",
            )

        lifecycle = await self._prove_lifecycle(publication_id)
        if lifecycle is None:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                int(publication_id),
                "ineligible",
            )

        candidate, result = await super()._load_candidate(
            publication_id,
            now=now,
            lock=lock,
        )
        if candidate is None:
            return None, result

        exact = (
            int(candidate.publication_id) == int(lifecycle.publication_id)
            and int(candidate.schedule_entry_id) == int(lifecycle.schedule_entry_id)
            and tuple(candidate.telegram_message_ids)
            == tuple(lifecycle.telegram_message_ids)
            and str(candidate.runtime_scheduled_at)
            == lifecycle.scheduled_at.isoformat()
            and bool(candidate.report_enabled) == bool(lifecycle.autodelete_report)
        )
        if not exact:
            await self.session.rollback()
            return None, PublicationAutodeleteResult(
                int(publication_id),
                "ineligible",
                message_count=len(candidate.telegram_message_ids),
            )
        return candidate, result
