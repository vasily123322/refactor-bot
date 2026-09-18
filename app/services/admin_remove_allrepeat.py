from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import Publication, ScheduleEntry


@dataclass(frozen=True, slots=True)
class AdminRemoveAllRepeatResult:
    removed_pending: int = 0
    disabled_flags: int = 0
    cleared_autodelete: int = 0
    protected_canonical: int = 0


def _runtime_options(meta: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    copied = deepcopy(dict(meta or {}))
    raw = copied.get("runtime_options")
    options = deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
    return copied, options


def _clear_pending_autodelete(meta: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
    copied, options = _runtime_options(meta)
    changed = False
    for key in ("autodelete_seconds", "autodelete_views", "autodelete_report"):
        if key in options:
            options.pop(key, None)
            changed = True
    if changed:
        if options:
            copied["runtime_options"] = options
        else:
            copied.pop("runtime_options", None)
    return copied, changed


class AdminRemoveAllRepeatService:
    """Bulk-clean only mutable canonical plans; historical PostTask rows are evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(self) -> AdminRemoveAllRepeatResult:
        rows = list(
            (
                await self.session.execute(
                    select(ScheduleEntry, Publication).outerjoin(
                        Publication,
                        Publication.schedule_entry_id == ScheduleEntry.id,
                    )
                )
            ).all()
        )

        removed_pending = 0
        disabled_flags = 0
        cleared_autodelete = 0
        protected_canonical = 0

        for schedule, publication in rows:
            rule = deepcopy(dict(schedule.repeat_rule or {}))
            schedule_meta = dict(schedule.meta or {})
            publication_meta = dict(publication.meta or {}) if publication is not None else {}
            repeat_related = bool(rule.get("enabled")) or (
                "repeat_group_id" in schedule_meta
                or "repeat_group_id" in publication_meta
            )

            mutable = str(schedule.status or "") == "pending" and (
                publication is None or str(publication.status or "") == "queued"
            )
            if not mutable:
                if repeat_related:
                    protected_canonical += 1
                continue

            if repeat_related:
                schedule.status = "cancelled"
                if publication is not None:
                    publication.status = "cancelled"
                    publication.last_error = None
                removed_pending += 1

            if bool(rule.get("enabled")) or "seconds" in rule:
                rule["enabled"] = False
                rule.pop("seconds", None)
                schedule.repeat_rule = rule
                disabled_flags += 1

            next_schedule_meta, schedule_cleared = _clear_pending_autodelete(
                schedule.meta
            )
            publication_cleared = False
            next_publication_meta: dict[str, Any] | None = None
            if publication is not None:
                next_publication_meta, publication_cleared = _clear_pending_autodelete(
                    publication.meta
                )
            if schedule_cleared:
                schedule.meta = next_schedule_meta
            if publication is not None and publication_cleared:
                publication.meta = next_publication_meta
            if schedule_cleared or publication_cleared:
                cleared_autodelete += 1

        await self.session.commit()
        return AdminRemoveAllRepeatResult(
            removed_pending=removed_pending,
            disabled_flags=disabled_flags,
            cleared_autodelete=cleared_autodelete,
            protected_canonical=protected_canonical,
        )
