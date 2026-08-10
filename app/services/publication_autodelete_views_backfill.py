from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.services.publication_autodelete_views_state import (
    PublicationAutodeleteViewStateService,
)
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.services.telegram_results import normalize_telegram_message_ids


@dataclass(frozen=True, slots=True)
class PublicationAutodeleteViewsBackfillBatch:
    scanned: int
    synced: int
    cleared: int
    invalid: int
    next_cursor: int
    done: bool


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return {}
    return value if isinstance(value, Mapping) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _threshold(options: Mapping[str, Any]) -> tuple[bool, int | None]:
    raw_views = options.get("autodelete_views")
    views = _positive_int(raw_views)
    if raw_views not in (None, False, 0, "0", "") and views is None:
        return False, None

    raw_seconds = options.get("autodelete_seconds")
    seconds = _positive_int(raw_seconds)
    if raw_seconds not in (None, False, 0, "0", "") and seconds is None:
        return False, None
    if views is not None and seconds is not None:
        return False, None
    return True, views


def _terminal_runtime(meta: Mapping[str, Any]) -> tuple[bool, bool]:
    raw = meta.get(AUTODELETE_RUNTIME_META_KEY)
    if raw is None:
        return True, False
    if not isinstance(raw, Mapping):
        return False, False
    deleted = raw.get("deleted")
    if deleted is not None and not isinstance(deleted, bool):
        return False, False
    return True, deleted is True


class PublicationAutodeleteViewsBackfillService:
    """Boundedly reconstruct indexed views state for historical published rows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def backfill_published(
        self,
        *,
        after_publication_id: int = 0,
        limit: int = 100,
        now: datetime | None = None,
    ) -> PublicationAutodeleteViewsBackfillBatch:
        try:
            cursor = max(0, int(after_publication_id))
        except (TypeError, ValueError, OverflowError):
            cursor = 0
        try:
            bounded_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 100

        rows = (
            await self.session.execute(
                select(Publication, PostTask)
                .outerjoin(PostTask, PostTask.id == Publication.legacy_post_task_id)
                .where(
                    Publication.id > cursor,
                    Publication.status == "published",
                )
                .order_by(Publication.id.asc())
                .limit(bounded_limit)
            )
        ).all()

        state_service = PublicationAutodeleteViewStateService(self.session)
        synced = 0
        cleared = 0
        invalid = 0
        next_cursor = cursor

        for publication, task in rows:
            publication_id = int(publication.id)
            next_cursor = max(next_cursor, publication_id)
            threshold: int | None = None
            valid = True

            meta = _mapping(publication.meta)
            if meta is None:
                valid = False
            else:
                runtime_valid, terminal_deleted = _terminal_runtime(meta)
                if not runtime_valid:
                    valid = False
                elif terminal_deleted:
                    threshold = None
                else:
                    ids = tuple(
                        normalize_telegram_message_ids(publication.telegram_message_ids)
                    )
                    if not ids:
                        valid = False
                    elif publication.legacy_post_task_id is not None:
                        if (
                            task is None
                            or int(task.id) != int(publication.legacy_post_task_id)
                            or int(task.channel_id) != int(publication.channel_id)
                            or str(task.status) != "done"
                        ):
                            valid = False
                        else:
                            payload = _mapping(task.payload)
                            if payload is None or tuple(
                                normalize_telegram_message_ids(payload.get("result_ids"))
                            ) != ids:
                                valid = False
                            else:
                                valid, threshold = _threshold(payload)
                    else:
                        raw_options = meta.get("runtime_options")
                        options = _mapping(raw_options)
                        if options is None:
                            valid = False
                        else:
                            valid, threshold = _threshold(options)

            if not valid:
                invalid += 1
                threshold = None

            snapshot = await state_service.sync_intent(
                publication_id=publication_id,
                threshold=threshold,
                now=now,
            )
            if snapshot is None:
                cleared += 1
            else:
                synced += 1

        if rows:
            await self.session.commit()

        return PublicationAutodeleteViewsBackfillBatch(
            scanned=len(rows),
            synced=synced,
            cleared=cleared,
            invalid=invalid,
            next_cursor=next_cursor,
            done=len(rows) < bounded_limit,
        )
