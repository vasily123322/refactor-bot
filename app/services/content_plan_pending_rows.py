from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.publication_execution_mode import CANONICAL_EXECUTION_MODE
from app.services.scheduling import as_utc


PendingContentPlanAuthority = Literal["canonical", "legacy"]


@dataclass(frozen=True, slots=True)
class PendingContentPlanRow:
    authority: PendingContentPlanAuthority
    scheduled_at: datetime
    title: str
    publication_id: int | None
    legacy_post_task_id: int | None
    runtime_options: dict[str, Any]
    repeat_enabled: bool
    repeat_seconds: int | None
    repeat_group_id: int | None

    @property
    def identity(self) -> tuple[str, int]:
        if self.authority == "canonical" and self.publication_id is not None:
            return ("publication", int(self.publication_id))
        if self.legacy_post_task_id is not None:
            return ("post_task", int(self.legacy_post_task_id))
        raise ValueError("pending content-plan row has no identity")


@dataclass(frozen=True, slots=True)
class PendingContentPlanPage:
    rows: tuple[PendingContentPlanRow, ...]
    total_count: int
    page: int
    total_pages: int


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _canonical_title(item: ContentItem, revision: ContentRevision) -> str:
    title = " ".join(str(item.title or "").split())
    if title:
        return title[:120]
    if isinstance(revision.document, dict):
        try:
            text = " ".join(PostDocument.from_dict(revision.document).primary_text().split())
        except Exception:
            text = ""
        if text:
            return text[:120]
    return "Без названия"


def _legacy_title(task: PostTask) -> str:
    payload = _mapping(task.payload)
    if payload.get("type") == "text":
        value = str(payload.get("text") or "")
    else:
        value = str(payload.get("caption") or payload.get("text") or "")
    first = " ".join(value.strip().splitlines()[:1]).strip()
    return (first or "Медиа")[:120]


def _repeat_from_canonical(
    publication: Publication,
    schedule: ScheduleEntry,
) -> tuple[bool, int | None, int | None]:
    rule = _mapping(schedule.repeat_rule)
    enabled = rule.get("enabled") is True
    seconds = _positive_int(rule.get("seconds")) if enabled else None
    if seconds is None:
        enabled = False
    schedule_meta = _mapping(schedule.meta)
    publication_meta = _mapping(publication.meta)
    group_id = (
        _positive_int(schedule_meta.get("repeat_group_id"))
        or _positive_int(publication_meta.get("repeat_group_id"))
    )
    return enabled, seconds, group_id


def _repeat_from_legacy(task: PostTask) -> tuple[bool, int | None, int | None]:
    payload = _mapping(task.payload)
    seconds = _positive_int(payload.get("repeat_seconds"))
    enabled = bool(payload.get("repeat_on")) and seconds is not None
    group_id = _positive_int(payload.get("repeat_group_id"))
    if enabled and group_id is None:
        group_id = _positive_int(task.id)
    return enabled, seconds if enabled else None, group_id


async def list_pending_content_plan_rows(
    session: AsyncSession,
    *,
    channel_id: int,
    tg_user_id: int,
    start_at: datetime,
    end_at: datetime,
    show_repeats: bool = True,
) -> list[PendingContentPlanRow]:
    """Return the pending Content Plan authority set before pagination.

    Canonical Publication/ScheduleEntry rows are primary. A PostTask is included only
    as a historical compatibility row when no canonical execution-mode Publication in
    this result explicitly links to that task. No payload/time/title heuristic links
    the two populations.
    """

    try:
        safe_channel_id = int(channel_id)
        safe_user_id = int(tg_user_id)
        start = as_utc(start_at)
        end = as_utc(end_at)
    except (TypeError, ValueError, OverflowError):
        return []
    if safe_channel_id <= 0 or safe_user_id <= 0 or end < start:
        return []

    canonical_rows = (
        await session.execute(
            select(Publication, ScheduleEntry, ContentItem, ContentRevision)
            .join(
                ScheduleEntry,
                and_(
                    ScheduleEntry.id == Publication.schedule_entry_id,
                    ScheduleEntry.channel_id == Publication.channel_id,
                    ScheduleEntry.content_item_id == Publication.content_item_id,
                    ScheduleEntry.content_revision == Publication.content_revision,
                ),
            )
            .join(Channel, Channel.id == Publication.channel_id)
            .join(Client, Client.id == Channel.owner_id)
            .join(
                ContentItem,
                and_(
                    ContentItem.id == Publication.content_item_id,
                    ContentItem.channel_id == Publication.channel_id,
                    ContentItem.kind == "post",
                ),
            )
            .join(
                ContentRevision,
                and_(
                    ContentRevision.content_item_id == Publication.content_item_id,
                    ContentRevision.revision == Publication.content_revision,
                ),
            )
            .where(
                Publication.channel_id == safe_channel_id,
                Client.tg_user_id == safe_user_id,
                Publication.execution_mode == CANONICAL_EXECUTION_MODE,
                Publication.status == "queued",
                ScheduleEntry.status == "pending",
                ScheduleEntry.scheduled_at >= start,
                ScheduleEntry.scheduled_at <= end,
            )
            .order_by(ScheduleEntry.scheduled_at.asc(), Publication.id.asc())
        )
    ).all()

    result: list[PendingContentPlanRow] = []
    linked_task_ids: set[int] = set()
    for publication, schedule, item, revision in canonical_rows:
        try:
            publication_id = int(publication.id)
            scheduled_at = as_utc(schedule.scheduled_at)
        except (TypeError, ValueError, OverflowError):
            continue
        if publication_id <= 0:
            continue
        if publication.legacy_post_task_id is not None:
            try:
                linked_task_id = int(publication.legacy_post_task_id)
            except (TypeError, ValueError, OverflowError):
                linked_task_id = 0
            if linked_task_id > 0:
                linked_task_ids.add(linked_task_id)
        repeat_enabled, repeat_seconds, repeat_group_id = _repeat_from_canonical(
            publication, schedule
        )
        result.append(
            PendingContentPlanRow(
                authority="canonical",
                scheduled_at=scheduled_at,
                title=_canonical_title(item, revision),
                publication_id=publication_id,
                legacy_post_task_id=(
                    int(publication.legacy_post_task_id)
                    if publication.legacy_post_task_id is not None
                    else None
                ),
                runtime_options=_mapping(_mapping(publication.meta).get("runtime_options")),
                repeat_enabled=repeat_enabled,
                repeat_seconds=repeat_seconds,
                repeat_group_id=repeat_group_id,
            )
        )

    # A canonical link suppresses its compatibility PostTask globally for the
    # channel, not only when the authoritative ScheduleEntry also falls inside this
    # day. Otherwise a canonical cross-day reschedule could resurrect a stale legacy
    # wrapper on the old day because PostTask.scheduled_at is intentionally no longer
    # authoritative.
    canonical_link_ids = (
        await session.execute(
            select(Publication.legacy_post_task_id)
            .join(Channel, Channel.id == Publication.channel_id)
            .join(Client, Client.id == Channel.owner_id)
            .where(
                Publication.channel_id == safe_channel_id,
                Client.tg_user_id == safe_user_id,
                Publication.execution_mode == CANONICAL_EXECUTION_MODE,
                Publication.legacy_post_task_id.is_not(None),
            )
        )
    ).scalars()
    for raw_task_id in canonical_link_ids:
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if task_id > 0:
            linked_task_ids.add(task_id)

    legacy_rows = list(
        (
            await session.execute(
                select(PostTask)
                .join(Channel, Channel.id == PostTask.channel_id)
                .join(Client, Client.id == Channel.owner_id)
                .where(
                    PostTask.channel_id == safe_channel_id,
                    Client.tg_user_id == safe_user_id,
                    PostTask.status == "pending",
                    PostTask.scheduled_at >= start,
                    PostTask.scheduled_at <= end,
                )
                .order_by(PostTask.scheduled_at.asc(), PostTask.id.asc())
            )
        ).scalars()
    )
    for task in legacy_rows:
        try:
            task_id = int(task.id)
            scheduled_at = as_utc(task.scheduled_at)
        except (TypeError, ValueError, OverflowError):
            continue
        if task_id <= 0 or task_id in linked_task_ids:
            continue
        repeat_enabled, repeat_seconds, repeat_group_id = _repeat_from_legacy(task)
        payload = _mapping(task.payload)
        result.append(
            PendingContentPlanRow(
                authority="legacy",
                scheduled_at=scheduled_at,
                title=_legacy_title(task),
                publication_id=None,
                legacy_post_task_id=task_id,
                runtime_options={
                    key: payload[key]
                    for key in (
                        "autodelete_seconds",
                        "autodelete_views",
                        "autodelete_report",
                    )
                    if key in payload
                },
                repeat_enabled=repeat_enabled,
                repeat_seconds=repeat_seconds,
                repeat_group_id=repeat_group_id,
            )
        )

    if not show_repeats:
        latest_by_group: dict[tuple[str, int], PendingContentPlanRow] = {}
        non_repeat: list[PendingContentPlanRow] = []
        for row in result:
            if not row.repeat_enabled or row.repeat_group_id is None:
                non_repeat.append(row)
                continue
            key = (row.authority, int(row.repeat_group_id))
            previous = latest_by_group.get(key)
            if previous is None or (row.scheduled_at, row.identity) > (
                previous.scheduled_at,
                previous.identity,
            ):
                latest_by_group[key] = row
        result = [*non_repeat, *latest_by_group.values()]

    authority_order = {"canonical": 0, "legacy": 1}
    result.sort(
        key=lambda row: (
            row.scheduled_at,
            authority_order[row.authority],
            row.identity[1],
        )
    )
    return result


def paginate_pending_content_plan_rows(
    rows: Sequence[PendingContentPlanRow],
    *,
    page: int,
    page_size: int = 8,
) -> PendingContentPlanPage:
    """Page an already deduplicated authority set."""

    safe_size = max(1, int(page_size))
    total_count = len(rows)
    total_pages = max(1, (total_count + safe_size - 1) // safe_size)
    safe_page = min(max(0, int(page)), total_pages - 1)
    start = safe_page * safe_size
    return PendingContentPlanPage(
        rows=tuple(rows[start : start + safe_size]),
        total_count=total_count,
        page=safe_page,
        total_pages=total_pages,
    )
