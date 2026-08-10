from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, ScheduleEntry


class PublicationAutodeleteViewStateError(ValueError):
    pass


class PublicationAutodeleteViewStateConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("canonical view autodelete state changed")


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewStateSnapshot:
    publication_id: int
    threshold: int
    last_views: int | None
    last_checked_at: datetime | None
    next_check_at: datetime


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewCandidateBatch:
    publication_ids: tuple[int, ...]
    done: bool


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise PublicationAutodeleteViewStateError(f"invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PublicationAutodeleteViewStateError(f"invalid {field}") from exc
    if parsed <= 0:
        raise PublicationAutodeleteViewStateError(f"invalid {field}")
    return parsed


def _optional_threshold(value: Any) -> int | None:
    if value in (None, "", 0, "0", False):
        return None
    return _positive_int(value, field="view threshold")


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise PublicationAutodeleteViewStateError(f"invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PublicationAutodeleteViewStateError(f"invalid {field}") from exc
    if parsed < 0:
        raise PublicationAutodeleteViewStateError(f"invalid {field}")
    return parsed


def _snapshot(state: PublicationAutodeleteViewState) -> PublicationAutodeleteViewStateSnapshot:
    return PublicationAutodeleteViewStateSnapshot(
        publication_id=int(state.publication_id),
        threshold=int(state.threshold),
        last_views=(int(state.last_views) if state.last_views is not None else None),
        last_checked_at=(
            _utc(state.last_checked_at) if state.last_checked_at is not None else None
        ),
        next_check_at=_utc(state.next_check_at),
    )


class PublicationAutodeleteViewStateService:
    """Maintain indexed scheduler facts for views-based canonical autodelete.

    Methods intentionally flush but do not commit. The caller owns the transaction so
    editor/publish runtime intent and scheduler state can be changed atomically later.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def sync_intent(
        self,
        *,
        publication_id: int,
        threshold: Any,
        now: datetime | None = None,
    ) -> PublicationAutodeleteViewStateSnapshot | None:
        safe_publication_id = _positive_int(publication_id, field="publication id")
        safe_threshold = _optional_threshold(threshold)
        current = _utc(now)

        publication = (
            await self.session.execute(
                select(Publication)
                .where(Publication.id == safe_publication_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None:
            raise PublicationAutodeleteViewStateError("publication not found")

        state = (
            await self.session.execute(
                select(PublicationAutodeleteViewState)
                .where(
                    PublicationAutodeleteViewState.publication_id
                    == safe_publication_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if safe_threshold is None:
            if state is not None:
                await self.session.delete(state)
                await self.session.flush()
            return None

        if state is None:
            state = PublicationAutodeleteViewState(
                publication_id=safe_publication_id,
                threshold=safe_threshold,
                last_views=None,
                last_checked_at=None,
                next_check_at=current,
            )
            self.session.add(state)
            await self.session.flush()
            return _snapshot(state)

        if int(state.threshold) != safe_threshold:
            state.threshold = safe_threshold
            state.last_views = None
            state.last_checked_at = None
            state.next_check_at = current
            await self.session.flush()

        return _snapshot(state)

    async def record_observation(
        self,
        *,
        publication_id: int,
        expected_threshold: Any,
        views: Any,
        checked_at: datetime,
        next_check_at: datetime,
    ) -> PublicationAutodeleteViewStateSnapshot:
        safe_publication_id = _positive_int(publication_id, field="publication id")
        safe_threshold = _positive_int(expected_threshold, field="view threshold")
        safe_views = _nonnegative_int(views, field="view count")
        checked = _utc(checked_at)
        next_check = _utc(next_check_at)
        if next_check < checked:
            raise PublicationAutodeleteViewStateError(
                "next view check cannot precede observation"
            )

        state = (
            await self.session.execute(
                select(PublicationAutodeleteViewState)
                .where(
                    PublicationAutodeleteViewState.publication_id
                    == safe_publication_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None or int(state.threshold) != safe_threshold:
            raise PublicationAutodeleteViewStateConflict()

        state.last_views = safe_views
        state.last_checked_at = checked
        state.next_check_at = next_check
        await self.session.flush()
        return _snapshot(state)

    async def select_due_publication_ids(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> PublicationAutodeleteViewCandidateBatch:
        current = _utc(now)
        try:
            bounded_limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 50

        rows = (
            await self.session.execute(
                select(PublicationAutodeleteViewState.publication_id)
                .join(
                    Publication,
                    Publication.id == PublicationAutodeleteViewState.publication_id,
                )
                .join(
                    ScheduleEntry,
                    and_(
                        ScheduleEntry.id == Publication.schedule_entry_id,
                        ScheduleEntry.channel_id == Publication.channel_id,
                        ScheduleEntry.content_item_id == Publication.content_item_id,
                        ScheduleEntry.content_revision == Publication.content_revision,
                        ScheduleEntry.status == "completed",
                    ),
                )
                .join(
                    ContentItem,
                    and_(
                        ContentItem.id == Publication.content_item_id,
                        ContentItem.channel_id == Publication.channel_id,
                        ContentItem.kind == "post",
                    ),
                )
                .where(
                    Publication.status == "published",
                    PublicationAutodeleteViewState.next_check_at <= current,
                )
                .order_by(
                    PublicationAutodeleteViewState.next_check_at.asc(),
                    PublicationAutodeleteViewState.publication_id.asc(),
                )
                .limit(bounded_limit)
            )
        ).scalars().all()

        publication_ids = tuple(int(value) for value in rows)
        return PublicationAutodeleteViewCandidateBatch(
            publication_ids=publication_ids,
            done=len(publication_ids) < bounded_limit,
        )
