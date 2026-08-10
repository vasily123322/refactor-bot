from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.repeat_runtime_intent import canonical_repeat_runtime_intent


@dataclass(frozen=True, slots=True)
class RepeatRuntimeIntentBackfillBatch:
    scanned: int = 0
    updated: int = 0
    skipped_existing: int = 0
    skipped_unproven: int = 0
    skipped_changed: int = 0
    failures: int = 0
    next_cursor: int = 0
    done: bool = False


def _meta(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


class RepeatRuntimeIntentBackfillService:
    """One-time bounded backfill for active linked repeat children.

    Newly mirrored repeat children receive canonical runtime intent at mirror time.
    This service only repairs rows that already existed before that projection. It
    never overwrites a canonical `runtime_options` key on either Publication or
    ScheduleEntry, even when only one side currently has a value.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _candidate_query(self, *, after_publication_id: int, limit: int):
        return (
            select(Publication.id)
            .join(
                ScheduleEntry,
                and_(
                    ScheduleEntry.id == Publication.schedule_entry_id,
                    ScheduleEntry.channel_id == Publication.channel_id,
                    ScheduleEntry.content_item_id == Publication.content_item_id,
                    ScheduleEntry.content_revision == Publication.content_revision,
                ),
            )
            .join(PostTask, PostTask.id == Publication.legacy_post_task_id)
            .where(
                Publication.id > int(after_publication_id),
                Publication.status.in_(("queued", "sending")),
            )
            .order_by(Publication.id.asc())
            .limit(max(1, min(int(limit), 500)))
        )

    async def _locked_candidate(
        self,
        publication_id: int,
    ) -> tuple[Publication, ScheduleEntry, PostTask] | None:
        return (
            await self.session.execute(
                select(Publication, ScheduleEntry, PostTask)
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                    ),
                )
                .join(PostTask, PostTask.id == Publication.legacy_post_task_id)
                .where(
                    Publication.id == int(publication_id),
                    Publication.status.in_(("queued", "sending")),
                )
                .with_for_update()
            )
        ).one_or_none()

    async def backfill_active(
        self,
        *,
        after_publication_id: int = 0,
        limit: int = 100,
    ) -> RepeatRuntimeIntentBackfillBatch:
        try:
            cursor = max(0, int(after_publication_id))
        except (TypeError, ValueError, OverflowError):
            cursor = 0
        bounded_limit = max(1, min(int(limit), 500))
        candidate_ids = [
            int(value)
            for value in (
                await self.session.execute(
                    self._candidate_query(
                        after_publication_id=cursor,
                        limit=bounded_limit,
                    )
                )
            ).scalars().all()
        ]

        updated = 0
        skipped_existing = 0
        skipped_unproven = 0
        skipped_changed = 0
        failures = 0

        for publication_id in candidate_ids:
            try:
                locked = await self._locked_candidate(publication_id)
                if locked is None:
                    await self.session.rollback()
                    skipped_changed += 1
                    continue
                publication, schedule, task = locked
                publication_meta = _meta(publication.meta)
                schedule_meta = _meta(schedule.meta)
                if publication_meta is None or schedule_meta is None:
                    await self.session.rollback()
                    skipped_unproven += 1
                    continue

                # Existing canonical state is authoritative. Never fill one side from
                # transport or overwrite a value that another migration already set.
                if (
                    "runtime_options" in publication_meta
                    or "runtime_options" in schedule_meta
                ):
                    await self.session.rollback()
                    skipped_existing += 1
                    continue

                intent = await canonical_repeat_runtime_intent(
                    self.session,
                    payload=dict(task.payload or {}),
                    channel_id=int(publication.channel_id),
                    task_id=int(task.id),
                    content_item_id=int(publication.content_item_id),
                    content_revision=int(publication.content_revision),
                )
                if not intent:
                    await self.session.rollback()
                    skipped_unproven += 1
                    continue

                publication.meta = {
                    **publication_meta,
                    "runtime_options": deepcopy(intent),
                    "repeat_runtime_intent_provenance": True,
                }
                schedule.meta = {
                    **schedule_meta,
                    "runtime_options": deepcopy(intent),
                    "repeat_runtime_intent_provenance": True,
                }
                await self.session.commit()
                updated += 1
            except Exception:
                await self.session.rollback()
                failures += 1

        next_cursor = candidate_ids[-1] if candidate_ids else cursor
        return RepeatRuntimeIntentBackfillBatch(
            scanned=len(candidate_ids),
            updated=updated,
            skipped_existing=skipped_existing,
            skipped_unproven=skipped_unproven,
            skipped_changed=skipped_changed,
            failures=failures,
            next_cursor=next_cursor,
            done=len(candidate_ids) < bounded_limit,
        )
