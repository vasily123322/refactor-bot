from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentItem, ContentRevision
from app.domain.models import PostTask
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.content import LegacyPayloadError, document_from_legacy_payload
from app.services.legacy_content_mirror import (
    _author_id,
    _content_payload,
    _repeat_rule,
    _title_from_document_text,
)
from app.services.posting_dedupe import acquire_posting_dedupe_lock
from app.services.publication_bridge import _delivery_meta
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    CANONICAL_SCHEDULING_OUTCOME,
    SchedulingBoundaryDecision,
    UnsupportedSchedulingProfileError,
    has_historical_scheduling_provenance,
    scheduling_boundary_from_legacy_payload,
)
from app.services.publication_runtime import (
    AUTODELETE_RUNTIME_META_KEY,
    normalize_autodelete_runtime,
)
from app.services.scheduling import as_utc


_POSTING_DEDUPE_META_KEY = "posting_dedupe_key"


async def find_direct_canonical_by_dedupe(
    session: AsyncSession,
    dedupe_key: str | None,
) -> Publication | None:
    """Return an already materialized direct canonical occurrence for one caller key."""

    if dedupe_key is None:
        return None
    return (
        await session.execute(
            select(Publication)
            .where(Publication.posting_dedupe_key == str(dedupe_key))
            .order_by(Publication.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _find_existing_posting_owner(
    session: AsyncSession,
    dedupe_key: str,
) -> PostTask | Publication | None:
    """Resolve the single current owner only after the shared dedupe mutex is held."""

    key = str(dedupe_key)
    legacy = (
        await session.execute(
            select(PostTask).where(PostTask.dedupe_key == key).limit(1)
        )
    ).scalar_one_or_none()
    if legacy is not None:
        return legacy
    return await find_direct_canonical_by_dedupe(session, key)


async def _locked_existing_owner(
    session: AsyncSession,
    *,
    dedupe_key: str | None,
    commit: bool,
) -> PostTask | Publication | None:
    if dedupe_key is None:
        return None
    await acquire_posting_dedupe_lock(session, str(dedupe_key))
    existing = await _find_existing_posting_owner(session, str(dedupe_key))
    if existing is not None and commit:
        # A standalone materializer owns its transaction when commit=True. Committing
        # here releases the mutex while preserving the already-existing owner unchanged.
        await session.commit()
        await session.refresh(existing)
    return existing


async def materialize_new_canonical_occurrence(
    session: AsyncSession,
    *,
    channel_id: int,
    payload: Mapping[str, Any],
    scheduled_at: datetime | None,
    dedupe_key: str | None = None,
    boundary: SchedulingBoundaryDecision | None = None,
    commit: bool = True,
) -> PostTask | Publication:
    """Persist one fresh canonical-owned schedule occurrence.

    This helper never authorizes retained legacy ownership and never uses ``None`` as an
    ownership signal. Fresh ingress must classify first, call this helper only for an
    explicit canonical decision, and handle an explicit allowlisted-legacy decision at
    the PostTask-creating boundary itself.
    """

    data = deepcopy(dict(payload or {}))
    decision = boundary or scheduling_boundary_from_legacy_payload(data)

    if decision.outcome != CANONICAL_SCHEDULING_OUTCOME:
        raise UnsupportedSchedulingProfileError(
            f"canonical materializer requires canonical scheduling outcome: {decision.reason}"
        )

    if decision.execution_mode != CANONICAL_EXECUTION_MODE:
        raise UnsupportedSchedulingProfileError(
            "canonical scheduling outcome has non-canonical execution mode"
        )

    if has_historical_scheduling_provenance(data):
        raise UnsupportedSchedulingProfileError(
            "fresh canonical scheduling does not accept legacy provenance"
        )

    runtime_options = decision.runtime_options
    if runtime_options is None:
        raise UnsupportedSchedulingProfileError(
            "canonical scheduling boundary produced no runtime options"
        )

    try:
        document = document_from_legacy_payload(_content_payload(data))
    except LegacyPayloadError as exc:
        raise UnsupportedSchedulingProfileError(
            "unsupported scheduling content payload"
        ) from exc

    existing = await _locked_existing_owner(
        session,
        dedupe_key=dedupe_key,
        commit=commit,
    )
    if existing is not None:
        return existing

    channel_pk = int(channel_id)
    when = as_utc(scheduled_at)
    rule = _repeat_rule(data)
    autodelete_runtime = normalize_autodelete_runtime(data)

    meta_seed: dict[str, Any] = {
        "scheduled_direct_canonical": True,
        "canonical_posttask_free": True,
    }
    if dedupe_key is not None:
        meta_seed[_POSTING_DEDUPE_META_KEY] = str(dedupe_key)
    if autodelete_runtime is not None:
        meta_seed[AUTODELETE_RUNTIME_META_KEY] = deepcopy(autodelete_runtime)
    canonical_meta = _delivery_meta(meta_seed, runtime_options)

    publication: Publication
    try:
        # Keep the whole direct graph behind a savepoint. The shared mutex serializes
        # PostingService cross-mode ownership; the Publication UNIQUE column remains an
        # independent same-store defense for direct helper callers and malformed races.
        async with session.begin_nested():
            item = ContentItem(
                channel_id=channel_pk,
                kind="post",
                status="ready",
                title=_title_from_document_text(document.primary_text()),
                current_revision=1,
                meta={"scheduled_direct_canonical": True},
            )
            session.add(item)
            await session.flush()

            revision = ContentRevision(
                content_item_id=int(item.id),
                revision=1,
                document=document.to_dict(),
                source="posting_schedule",
                created_by_tg_user_id=_author_id(data),
                meta={"scheduled_direct_canonical": True},
            )
            session.add(revision)

            schedule = ScheduleEntry(
                content_item_id=int(item.id),
                content_revision=1,
                channel_id=channel_pk,
                scheduled_at=when,
                timezone=None,
                status="pending",
                repeat_rule=deepcopy(rule),
                meta=deepcopy(canonical_meta),
            )
            publication = Publication(
                schedule_entry_id=None,
                content_item_id=int(item.id),
                content_revision=1,
                channel_id=channel_pk,
                status="queued",
                execution_mode=CANONICAL_EXECUTION_MODE,
                posting_dedupe_key=(
                    str(dedupe_key) if dedupe_key is not None else None
                ),
                legacy_post_task_id=None,
                meta=deepcopy(canonical_meta),
            )
            session.add_all([schedule, publication])

            await session.flush()
            publication.schedule_entry_id = int(schedule.id)

            if rule.get("enabled") is True:
                # A direct canonical repeat root has no PostTask identity. Its durable
                # Publication identity is therefore the root group anchor consumed by the
                # already PostTask-free Stage 5 continuation path.
                repeat_group_id = int(publication.id)
                publication.meta = {
                    **deepcopy(dict(publication.meta or {})),
                    "repeat_group_id": repeat_group_id,
                }
                schedule.meta = {
                    **deepcopy(dict(schedule.meta or {})),
                    "repeat_group_id": repeat_group_id,
                }
                await session.flush()
    except IntegrityError:
        if dedupe_key is not None:
            existing = await _find_existing_posting_owner(session, str(dedupe_key))
            if existing is not None:
                if commit:
                    await session.commit()
                    await session.refresh(existing)
                return existing
        if commit:
            await session.rollback()
        raise
    except Exception:
        if commit:
            await session.rollback()
        raise

    if commit:
        try:
            await session.commit()
            await session.refresh(publication)
        except Exception:
            await session.rollback()
            raise
    return publication
