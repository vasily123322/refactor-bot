from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import localize_wall_clock_strict
from app.domain.admin_agent import (
    AdminAgentApprovalBatch,
    AdminAgentApprovalBatchItem,
    AdminAgentRun,
    AdminAgentRunArtifact,
)
from app.domain.content import PostDocument, validate_native_document_capabilities
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.admin_agent import (
    RUN_COMPLETED,
    SCENARIO_PREPARE_CONTENT_SERIES,
    _resolve_channel_timezone,
)
from app.services.admin_agent_execution_fence import fence_execution_claim
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import as_utc


ACTION_SCHEDULE_CONTENT_SERIES = "schedule_content_series"
SOURCE_ARTIFACT_TYPE = "series_draft"
SUPPORTED_SKILL_ID = "prepare_content_series"
SUPPORTED_SKILL_VERSION = "1"

STATE_PENDING_REVIEW = "pending_review"
STATE_EXECUTING = "executing"
STATE_EXECUTED = "executed"
STATE_REJECTED = "rejected"
STATE_STALE = "stale"
STATE_PARTIAL_FAILED = "partial_failed"
STATE_FAILED = "failed"

ITEM_PENDING = "pending"
ITEM_EXECUTING = "executing"
ITEM_EXECUTED = "executed"
ITEM_STALE = "stale"
ITEM_FAILED = "failed"

_EXECUTION_CLAIM_SECONDS = 30
_ELIGIBLE_CONTENT_STATUSES = frozenset({"draft"})


class SeriesApprovalInputError(ValueError):
    pass


class SeriesApprovalIdempotencyConflict(RuntimeError):
    pass


class SeriesApprovalStateConflict(RuntimeError):
    pass


class SeriesApprovalExecutionError(RuntimeError):
    pass


class _CanonicalApprovalTargetStale(RuntimeError):
    pass


def _normalized_now(value: datetime | None = None) -> datetime:
    return as_utc(value or datetime.now(timezone.utc))


def _claim_token() -> str:
    return sha256(uuid4().hex.encode("ascii")).hexdigest()


def _strict_date(value: object) -> date:
    raw = str(value or "")
    if len(raw) != 10:
        raise SeriesApprovalInputError("local_date must use YYYY-MM-DD")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SeriesApprovalInputError("local_date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise SeriesApprovalInputError("local_date must use YYYY-MM-DD")
    return parsed


def _strict_time(value: object) -> time:
    raw = str(value or "")
    if len(raw) != 5:
        raise SeriesApprovalInputError("local_time must use HH:MM")
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise SeriesApprovalInputError("local_time must use HH:MM") from exc
    if parsed.strftime("%H:%M") != raw:
        raise SeriesApprovalInputError("local_time must use HH:MM")
    return parsed


def _hash_payload(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _item_fingerprint(
    *,
    owner_tg_user_id: int,
    channel_id: int,
    source_run_id: int,
    source_plan_fingerprint: str,
    ordinal: int,
    content_item_id: int,
    captured_content_revision: int,
    timezone_name: str,
    local_date_value: date,
    local_time_value: str,
    scheduled_at: datetime,
) -> str:
    return _hash_payload(
        {
            "action_type": ACTION_SCHEDULE_CONTENT_SERIES,
            "owner_tg_user_id": int(owner_tg_user_id),
            "channel_id": int(channel_id),
            "source_run_id": int(source_run_id),
            "source_plan_fingerprint": str(source_plan_fingerprint),
            "ordinal": int(ordinal),
            "content_item_id": int(content_item_id),
            "captured_content_revision": int(captured_content_revision),
            "timezone": str(timezone_name),
            "local_date": local_date_value.isoformat(),
            "local_time": str(local_time_value),
            "scheduled_at_utc": as_utc(scheduled_at).isoformat(),
        }
    )


def _batch_fingerprint(
    *,
    owner_tg_user_id: int,
    channel_id: int,
    source_run_id: int,
    source_plan_fingerprint: str,
    timezone_name: str,
    items: list[dict[str, object]],
) -> str:
    return _hash_payload(
        {
            "action_type": ACTION_SCHEDULE_CONTENT_SERIES,
            "owner_tg_user_id": int(owner_tg_user_id),
            "channel_id": int(channel_id),
            "source_run_id": int(source_run_id),
            "source_plan_fingerprint": str(source_plan_fingerprint),
            "timezone": str(timezone_name),
            "items": [
                {
                    "ordinal": int(row["ordinal"]),
                    "content_item_id": int(row["content_item_id"]),
                    "captured_content_revision": int(row["captured_content_revision"]),
                    "local_date": str(row["local_date"]),
                    "local_time": str(row["local_time"]),
                    "scheduled_at_utc": str(row["scheduled_at_utc"]),
                    "item_fingerprint": str(row["item_fingerprint"]),
                }
                for row in items
            ],
        }
    )


def _batch_execution_key(
    *,
    owner_tg_user_id: int,
    channel_id: int,
    source_run_id: int,
    request_id: str,
    action_fingerprint: str,
) -> str:
    raw = (
        f"admin-agent-series-batch:{int(owner_tg_user_id)}:{int(channel_id)}:"
        f"{int(source_run_id)}:{str(request_id)}:{str(action_fingerprint)}"
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _item_execution_key(
    *,
    batch_execution_key: str,
    ordinal: int,
    item_fingerprint: str,
) -> str:
    raw = (
        f"admin-agent-series-item:{str(batch_execution_key)}:"
        f"{int(ordinal)}:{str(item_fingerprint)}"
    )
    return sha256(raw.encode("utf-8")).hexdigest()


class AdminAgentSeriesApprovalService:
    """Approval-gated, recoverable scheduling for one prepared content series."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        now_utc: datetime | None = None,
    ):
        self.session = session
        self.now_utc = _normalized_now(now_utc)

    async def _load(
        self,
        *,
        batch_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        for_update: bool = False,
    ) -> AdminAgentApprovalBatch | None:
        stmt = (
            select(AdminAgentApprovalBatch)
            .where(
                AdminAgentApprovalBatch.id == int(batch_id),
                AdminAgentApprovalBatch.owner_tg_user_id == int(owner_tg_user_id),
                AdminAgentApprovalBatch.channel_id == int(channel_id),
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get(
        self,
        *,
        batch_id: int,
        owner_tg_user_id: int,
        channel_id: int,
    ) -> AdminAgentApprovalBatch | None:
        return await self._load(
            batch_id=batch_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
        )

    async def list(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        limit: int = 50,
    ) -> list[AdminAgentApprovalBatch]:
        return list(
            (
                await self.session.execute(
                    select(AdminAgentApprovalBatch)
                    .where(
                        AdminAgentApprovalBatch.owner_tg_user_id
                        == int(owner_tg_user_id),
                        AdminAgentApprovalBatch.channel_id == int(channel_id),
                    )
                    .order_by(AdminAgentApprovalBatch.id.desc())
                    .limit(max(1, min(int(limit), 50)))
                )
            ).scalars()
        )

    async def latest_for_source_run(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        source_run_id: int,
    ) -> AdminAgentApprovalBatch | None:
        return (
            await self.session.execute(
                select(AdminAgentApprovalBatch)
                .where(
                    AdminAgentApprovalBatch.owner_tg_user_id == int(owner_tg_user_id),
                    AdminAgentApprovalBatch.channel_id == int(channel_id),
                    AdminAgentApprovalBatch.source_run_id == int(source_run_id),
                    AdminAgentApprovalBatch.action_type == ACTION_SCHEDULE_CONTENT_SERIES,
                )
                .order_by(AdminAgentApprovalBatch.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def items_for_batch(
        self,
        batch_id: int,
    ) -> list[AdminAgentApprovalBatchItem]:
        return list(
            (
                await self.session.execute(
                    select(AdminAgentApprovalBatchItem)
                    .where(AdminAgentApprovalBatchItem.batch_id == int(batch_id))
                    .order_by(AdminAgentApprovalBatchItem.ordinal.asc())
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )

    async def _revision_row(
        self,
        *,
        content_item_id: int,
        content_revision: int,
    ) -> ContentRevision | None:
        return (
            await self.session.execute(
                select(ContentRevision).where(
                    ContentRevision.content_item_id == int(content_item_id),
                    ContentRevision.revision == int(content_revision),
                )
            )
        ).scalar_one_or_none()

    async def _validate_document(
        self,
        *,
        content_item_id: int,
        content_revision: int,
    ) -> None:
        revision = await self._revision_row(
            content_item_id=content_item_id,
            content_revision=content_revision,
        )
        if revision is None:
            raise SeriesApprovalInputError("content revision not found")
        try:
            document = PostDocument.from_dict(revision.document)
            document.validate()
            validate_native_document_capabilities(document)
        except Exception as exc:
            raise SeriesApprovalInputError("content document is invalid") from exc

    async def _source_snapshot(
        self,
        *,
        source_run_id: int,
        owner_tg_user_id: int,
        channel_id: int,
    ) -> dict[str, object]:
        run = (
            await self.session.execute(
                select(AdminAgentRun)
                .where(AdminAgentRun.id == int(source_run_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            run is None
            or int(run.owner_tg_user_id) != int(owner_tg_user_id)
            or int(run.channel_id) != int(channel_id)
        ):
            raise SeriesApprovalInputError("source run not found")
        if str(run.scenario) != SCENARIO_PREPARE_CONTENT_SERIES:
            raise SeriesApprovalInputError("source run is not a content-series run")
        if (
            str(run.skill_id or "") != SUPPORTED_SKILL_ID
            or str(run.skill_version or "") != SUPPORTED_SKILL_VERSION
        ):
            raise SeriesApprovalInputError("source run skill/version is unsupported")
        if (
            str(run.status) != RUN_COMPLETED
            or str(run.workflow_phase or "") != "completed"
            or run.checkpoint is not None
        ):
            raise SeriesApprovalInputError("source run is not completed")

        operator_input = run.operator_input
        if not isinstance(operator_input, dict) or set(operator_input) != {
            "brief",
            "post_count",
        }:
            raise SeriesApprovalInputError("source run operator input is malformed")
        post_count = operator_input.get("post_count")
        if (
            isinstance(post_count, bool)
            or not isinstance(post_count, int)
            or not 2 <= post_count <= 8
        ):
            raise SeriesApprovalInputError("source run post count is malformed")

        result = run.result
        if not isinstance(result, dict):
            raise SeriesApprovalInputError("source run result is malformed")
        source_plan_fingerprint = result.get("plan_fingerprint")
        series_title = result.get("series_title")
        result_posts = result.get("posts")
        if (
            result.get("scenario") != SCENARIO_PREPARE_CONTENT_SERIES
            or result.get("requested_post_count") != post_count
            or not isinstance(source_plan_fingerprint, str)
            or len(source_plan_fingerprint) != 64
            or not isinstance(series_title, str)
            or not series_title.strip()
            or not isinstance(result_posts, list)
            or len(result_posts) != post_count
        ):
            raise SeriesApprovalInputError("source run result is malformed")
        if [
            row.get("ordinal") if isinstance(row, dict) else None
            for row in result_posts
        ] != list(range(1, post_count + 1)):
            raise SeriesApprovalInputError("source run result ordinals are malformed")

        artifacts = list(
            (
                await self.session.execute(
                    select(AdminAgentRunArtifact)
                    .where(AdminAgentRunArtifact.run_id == int(run.id))
                    .order_by(AdminAgentRunArtifact.ordinal.asc())
                )
            ).scalars()
        )
        if (
            len(artifacts) != post_count
            or [int(row.ordinal) for row in artifacts]
            != list(range(1, post_count + 1))
            or any(
                str(row.artifact_type) != SOURCE_ARTIFACT_TYPE for row in artifacts
            )
            or len({int(row.content_item_id) for row in artifacts}) != post_count
        ):
            raise SeriesApprovalInputError("source series artifacts are malformed")

        content_ids = [int(row.content_item_id) for row in artifacts]
        content_rows = list(
            (
                await self.session.execute(
                    select(ContentItem).where(ContentItem.id.in_(content_ids))
                )
            ).scalars()
        )
        by_id = {int(row.id): row for row in content_rows}
        if len(by_id) != post_count:
            raise SeriesApprovalInputError("source series content is missing")

        captured: list[dict[str, object]] = []
        for artifact, result_post in zip(artifacts, result_posts, strict=True):
            item = by_id.get(int(artifact.content_item_id))
            ordinal = int(artifact.ordinal)
            if item is None:
                raise SeriesApprovalInputError("source series content is missing")
            meta = dict(item.meta or {})
            if (
                int(item.channel_id) != int(channel_id)
                or str(item.kind) != "post"
                or str(item.status) not in _ELIGIBLE_CONTENT_STATUSES
                or int(item.current_revision or 0) <= 0
                or int(meta.get("admin_agent_run_id") or 0) != int(run.id)
                or str(meta.get("admin_agent_scenario") or "")
                != SCENARIO_PREPARE_CONTENT_SERIES
                or str(meta.get("skill_id") or "") != SUPPORTED_SKILL_ID
                or str(meta.get("skill_version") or "") != SUPPORTED_SKILL_VERSION
                or int(meta.get("series_ordinal") or 0) != ordinal
                or str(meta.get("plan_fingerprint") or "")
                != str(source_plan_fingerprint)
            ):
                raise SeriesApprovalInputError("source series content provenance is invalid")
            original_revision = await self._revision_row(
                content_item_id=int(item.id),
                content_revision=int(artifact.content_revision),
            )
            expected_provenance = {
                "admin_agent_run_id": int(run.id),
                "admin_agent_scenario": SCENARIO_PREPARE_CONTENT_SERIES,
                "skill_id": SUPPORTED_SKILL_ID,
                "skill_version": SUPPORTED_SKILL_VERSION,
                "series_ordinal": ordinal,
                "plan_fingerprint": str(source_plan_fingerprint),
            }
            original_document = (
                original_revision.document
                if original_revision is not None
                and isinstance(original_revision.document, dict)
                else {}
            )
            if (
                original_revision is None
                or str(original_revision.source) != "admin_agent"
                or dict(original_revision.meta or {}) != expected_provenance
                or dict(original_document.get("metadata") or {}) != expected_provenance
                or not isinstance(result_post, dict)
                or int(result_post.get("content_item_id") or 0) != int(item.id)
                or int(result_post.get("content_revision") or 0)
                != int(artifact.content_revision)
            ):
                raise SeriesApprovalInputError(
                    "source series artifact revision provenance is invalid"
                )

            captured_revision = int(item.current_revision)
            await self._validate_document(
                content_item_id=int(item.id),
                content_revision=captured_revision,
            )
            result_title = (
                str(result_post.get("title") or "").strip()
                if isinstance(result_post, dict)
                else ""
            )
            captured.append(
                {
                    "ordinal": ordinal,
                    "content_item_id": int(item.id),
                    "captured_content_revision": captured_revision,
                    "content_title": str(item.title or result_title or f"Post {ordinal}")[
                        :255
                    ],
                }
            )

        return {
            "run": run,
            "post_count": post_count,
            "series_title": series_title.strip()[:255],
            "source_plan_fingerprint": source_plan_fingerprint,
            "items": captured,
        }

    async def _normalize_slots(
        self,
        *,
        snapshot: dict[str, object],
        slots: list[dict[str, object]],
        timezone_name: str,
    ) -> list[dict[str, object]]:
        post_count = int(snapshot["post_count"])
        if len(slots) != post_count:
            raise SeriesApprovalInputError(
                "slots must contain exactly one entry for every series ordinal"
            )
        expected_ordinals = list(range(1, post_count + 1))
        seen: set[int] = set()
        normalized_by_ordinal: dict[int, tuple[date, str, datetime]] = {}
        seen_datetimes: set[datetime] = set()
        for raw_slot in slots:
            if not isinstance(raw_slot, dict) or set(raw_slot) != {
                "ordinal",
                "local_date",
                "local_time",
            }:
                raise SeriesApprovalInputError("slot fields are invalid")
            raw_ordinal = raw_slot.get("ordinal")
            if isinstance(raw_ordinal, bool):
                raise SeriesApprovalInputError("slot ordinal is invalid")
            try:
                ordinal = int(raw_ordinal)
            except (TypeError, ValueError) as exc:
                raise SeriesApprovalInputError("slot ordinal is invalid") from exc
            if ordinal != raw_ordinal or ordinal not in expected_ordinals:
                raise SeriesApprovalInputError("slot ordinal is invalid")
            if ordinal in seen:
                raise SeriesApprovalInputError("slot ordinal is duplicated")
            seen.add(ordinal)

            local_date_value = _strict_date(raw_slot.get("local_date"))
            parsed_time = _strict_time(raw_slot.get("local_time"))
            local_time_value = parsed_time.strftime("%H:%M")
            try:
                scheduled_at = localize_wall_clock_strict(
                    datetime.combine(local_date_value, parsed_time),
                    timezone_name,
                ).astimezone(timezone.utc)
            except ValueError as exc:
                raise SeriesApprovalInputError(str(exc)) from exc
            scheduled_at = as_utc(scheduled_at)
            if scheduled_at <= self.now_utc:
                raise SeriesApprovalInputError("all schedule slots must be in the future")
            if scheduled_at in seen_datetimes:
                raise SeriesApprovalInputError(
                    "duplicate exact schedule datetimes are not allowed"
                )
            seen_datetimes.add(scheduled_at)
            normalized_by_ordinal[ordinal] = (
                local_date_value,
                local_time_value,
                scheduled_at,
            )

        if sorted(seen) != expected_ordinals:
            raise SeriesApprovalInputError(
                "slots must contain exactly one entry for every series ordinal"
            )

        source_items = snapshot["items"]
        assert isinstance(source_items, list)
        rows: list[dict[str, object]] = []
        for source_item in source_items:
            assert isinstance(source_item, dict)
            ordinal = int(source_item["ordinal"])
            local_date_value, local_time_value, scheduled_at = normalized_by_ordinal[
                ordinal
            ]
            item_fingerprint = _item_fingerprint(
                owner_tg_user_id=int(snapshot["run"].owner_tg_user_id),
                channel_id=int(snapshot["run"].channel_id),
                source_run_id=int(snapshot["run"].id),
                source_plan_fingerprint=str(snapshot["source_plan_fingerprint"]),
                ordinal=ordinal,
                content_item_id=int(source_item["content_item_id"]),
                captured_content_revision=int(
                    source_item["captured_content_revision"]
                ),
                timezone_name=timezone_name,
                local_date_value=local_date_value,
                local_time_value=local_time_value,
                scheduled_at=scheduled_at,
            )
            rows.append(
                {
                    **source_item,
                    "local_date_value": local_date_value,
                    "local_date": local_date_value.isoformat(),
                    "local_time": local_time_value,
                    "scheduled_at": scheduled_at,
                    "scheduled_at_utc": scheduled_at.isoformat(),
                    "item_fingerprint": item_fingerprint,
                }
            )
        return rows

    async def _existing_request_matches(
        self,
        existing: AdminAgentApprovalBatch,
        *,
        source_run_id: int,
        slots: list[dict[str, object]],
    ) -> bool:
        if int(existing.source_run_id) != int(source_run_id):
            return False
        existing_items = await self.items_for_batch(int(existing.id))
        if len(slots) != len(existing_items):
            return False
        artifacts = list(
            (
                await self.session.execute(
                    select(AdminAgentRunArtifact)
                    .where(
                        AdminAgentRunArtifact.run_id == int(source_run_id),
                        AdminAgentRunArtifact.artifact_type == SOURCE_ARTIFACT_TYPE,
                    )
                    .order_by(AdminAgentRunArtifact.ordinal.asc())
                )
            ).scalars()
        )
        if [
            (int(row.ordinal), int(row.content_item_id))
            for row in artifacts
        ] != [
            (int(item.ordinal), int(item.content_item_id))
            for item in existing_items
        ]:
            return False
        normalized: dict[int, tuple[str, str]] = {}
        try:
            for raw_slot in slots:
                if not isinstance(raw_slot, dict) or set(raw_slot) != {
                    "ordinal",
                    "local_date",
                    "local_time",
                }:
                    return False
                raw_ordinal = raw_slot.get("ordinal")
                if isinstance(raw_ordinal, bool):
                    return False
                ordinal = int(raw_ordinal)
                if ordinal != raw_ordinal or ordinal in normalized:
                    return False
                local_date_value = _strict_date(raw_slot.get("local_date"))
                local_time_value = _strict_time(raw_slot.get("local_time")).strftime("%H:%M")
                normalized[ordinal] = (
                    local_date_value.isoformat(),
                    local_time_value,
                )
        except (SeriesApprovalInputError, TypeError, ValueError):
            return False
        return normalized == {
            int(item.ordinal): (item.local_date.isoformat(), str(item.local_time))
            for item in existing_items
        }

    async def create(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        source_run_id: int,
        request_id: str,
        slots: list[dict[str, object]],
    ) -> AdminAgentApprovalBatch:
        existing = (
            await self.session.execute(
                select(AdminAgentApprovalBatch).where(
                    AdminAgentApprovalBatch.owner_tg_user_id
                    == int(owner_tg_user_id),
                    AdminAgentApprovalBatch.channel_id == int(channel_id),
                    AdminAgentApprovalBatch.action_type
                    == ACTION_SCHEDULE_CONTENT_SERIES,
                    AdminAgentApprovalBatch.request_id == str(request_id),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if await self._existing_request_matches(
                existing,
                source_run_id=source_run_id,
                slots=slots,
            ):
                return existing
            raise SeriesApprovalIdempotencyConflict(
                "request_id already exists with different series schedule intent"
            )

        snapshot = await self._source_snapshot(
            source_run_id=source_run_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
        )
        timezone_name = await _resolve_channel_timezone(self.session, int(channel_id))
        normalized_items = await self._normalize_slots(
            snapshot=snapshot,
            slots=slots,
            timezone_name=timezone_name,
        )
        action_fingerprint = _batch_fingerprint(
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            source_run_id=source_run_id,
            source_plan_fingerprint=str(snapshot["source_plan_fingerprint"]),
            timezone_name=timezone_name,
            items=normalized_items,
        )
        execution_key = _batch_execution_key(
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            source_run_id=source_run_id,
            request_id=request_id,
            action_fingerprint=action_fingerprint,
        )

        # End the validation-only transaction before the insert. On SQLite this
        # avoids two concurrent readers contending while upgrading to writers;
        # the partial unique index remains the durable race authority.
        await self.session.commit()
        batch = AdminAgentApprovalBatch(
            owner_tg_user_id=int(owner_tg_user_id),
            channel_id=int(channel_id),
            source_run_id=int(source_run_id),
            action_type=ACTION_SCHEDULE_CONTENT_SERIES,
            state=STATE_PENDING_REVIEW,
            request_id=str(request_id),
            timezone=str(timezone_name),
            item_count=int(snapshot["post_count"]),
            series_title=str(snapshot["series_title"]),
            source_plan_fingerprint=str(snapshot["source_plan_fingerprint"]),
            action_fingerprint=action_fingerprint,
            execution_key=execution_key,
            reviewer_tg_user_id=None,
            execution_claim_token=None,
            execution_claimed_at=None,
            failure_reason=None,
            reviewed_at=None,
            executed_at=None,
        )
        self.session.add(batch)
        try:
            await self.session.flush()
            for row in normalized_items:
                self.session.add(
                    AdminAgentApprovalBatchItem(
                        batch_id=int(batch.id),
                        ordinal=int(row["ordinal"]),
                        content_item_id=int(row["content_item_id"]),
                        captured_content_revision=int(
                            row["captured_content_revision"]
                        ),
                        content_title=str(row["content_title"]),
                        local_date=row["local_date_value"],
                        local_time=str(row["local_time"]),
                        resolved_scheduled_at=row["scheduled_at"],
                        item_fingerprint=str(row["item_fingerprint"]),
                        execution_key=_item_execution_key(
                            batch_execution_key=execution_key,
                            ordinal=int(row["ordinal"]),
                            item_fingerprint=str(row["item_fingerprint"]),
                        ),
                        state=ITEM_PENDING,
                        schedule_entry_id=None,
                        publication_id=None,
                        failure_reason=None,
                        execution_started_at=None,
                        executed_at=None,
                    )
                )
            await self.session.commit()
            await self.session.refresh(batch)
            return batch
        except IntegrityError:
            await self.session.rollback()
            retry = (
                await self.session.execute(
                    select(AdminAgentApprovalBatch).where(
                        AdminAgentApprovalBatch.owner_tg_user_id
                        == int(owner_tg_user_id),
                        AdminAgentApprovalBatch.channel_id == int(channel_id),
                        AdminAgentApprovalBatch.action_type
                        == ACTION_SCHEDULE_CONTENT_SERIES,
                        AdminAgentApprovalBatch.request_id == str(request_id),
                    )
                )
            ).scalar_one_or_none()
            if (
                retry is not None
                and str(retry.action_fingerprint) == action_fingerprint
                and int(retry.source_run_id) == int(source_run_id)
            ):
                return retry
            if retry is not None:
                raise SeriesApprovalIdempotencyConflict(
                    "request_id already exists with different series schedule intent"
                )
            active = (
                await self.session.execute(
                    select(AdminAgentApprovalBatch).where(
                        AdminAgentApprovalBatch.owner_tg_user_id
                        == int(owner_tg_user_id),
                        AdminAgentApprovalBatch.channel_id == int(channel_id),
                        AdminAgentApprovalBatch.action_type
                        == ACTION_SCHEDULE_CONTENT_SERIES,
                        AdminAgentApprovalBatch.source_run_id == int(source_run_id),
                        AdminAgentApprovalBatch.state.in_(
                            [STATE_PENDING_REVIEW, STATE_EXECUTING]
                        ),
                    )
                )
            ).scalar_one_or_none()
            if active is not None:
                raise SeriesApprovalStateConflict(
                    "an active series approval already exists for this source run"
                )
            raise

    async def _conflicting_schedule_exists(
        self,
        *,
        content_item_id: int,
        content_revision: int,
    ) -> bool:
        row = (
            await self.session.execute(
                select(ScheduleEntry.id)
                .where(
                    ScheduleEntry.content_item_id == int(content_item_id),
                    ScheduleEntry.content_revision == int(content_revision),
                    ScheduleEntry.status != "cancelled",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return row is not None

    async def _source_contract_reason(
        self,
        batch: AdminAgentApprovalBatch,
        items: list[AdminAgentApprovalBatchItem],
    ) -> str | None:
        run = (
            await self.session.execute(
                select(AdminAgentRun)
                .where(AdminAgentRun.id == int(batch.source_run_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if run is None:
            return "source run was removed"
        if (
            int(run.owner_tg_user_id) != int(batch.owner_tg_user_id)
            or int(run.channel_id) != int(batch.channel_id)
            or str(run.scenario) != SCENARIO_PREPARE_CONTENT_SERIES
            or str(run.skill_id or "") != SUPPORTED_SKILL_ID
            or str(run.skill_version or "") != SUPPORTED_SKILL_VERSION
            or str(run.status) != RUN_COMPLETED
            or str(run.workflow_phase or "") != "completed"
            or run.checkpoint is not None
        ):
            return "source run contract changed"
        result = run.result
        if (
            not isinstance(result, dict)
            or result.get("requested_post_count") != int(batch.item_count)
            or str(result.get("plan_fingerprint") or "")
            != str(batch.source_plan_fingerprint)
            or str(result.get("series_title") or "").strip()[:255]
            != str(batch.series_title)
        ):
            return "source run result changed"

        artifacts = list(
            (
                await self.session.execute(
                    select(AdminAgentRunArtifact)
                    .where(AdminAgentRunArtifact.run_id == int(batch.source_run_id))
                    .order_by(AdminAgentRunArtifact.ordinal.asc())
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if (
            len(artifacts) != len(items)
            or any(str(row.artifact_type) != SOURCE_ARTIFACT_TYPE for row in artifacts)
            or [int(row.ordinal) for row in artifacts]
            != [int(item.ordinal) for item in items]
            or [int(row.content_item_id) for row in artifacts]
            != [int(item.content_item_id) for item in items]
        ):
            return "source series artifacts changed"

        fingerprint_items = [
            {
                "ordinal": int(item.ordinal),
                "content_item_id": int(item.content_item_id),
                "captured_content_revision": int(item.captured_content_revision),
                "local_date": item.local_date.isoformat(),
                "local_time": str(item.local_time),
                "scheduled_at_utc": as_utc(item.resolved_scheduled_at).isoformat(),
                "item_fingerprint": str(item.item_fingerprint),
            }
            for item in items
        ]
        expected_batch_fingerprint = _batch_fingerprint(
            owner_tg_user_id=int(batch.owner_tg_user_id),
            channel_id=int(batch.channel_id),
            source_run_id=int(batch.source_run_id),
            source_plan_fingerprint=str(batch.source_plan_fingerprint),
            timezone_name=str(batch.timezone),
            items=fingerprint_items,
        )
        if expected_batch_fingerprint != str(batch.action_fingerprint):
            return "batch fingerprint mismatch"
        expected_execution_key = _batch_execution_key(
            owner_tg_user_id=int(batch.owner_tg_user_id),
            channel_id=int(batch.channel_id),
            source_run_id=int(batch.source_run_id),
            request_id=str(batch.request_id),
            action_fingerprint=str(batch.action_fingerprint),
        )
        if expected_execution_key != str(batch.execution_key):
            return "batch execution key mismatch"
        for item in items:
            expected_item_fingerprint = _item_fingerprint(
                owner_tg_user_id=int(batch.owner_tg_user_id),
                channel_id=int(batch.channel_id),
                source_run_id=int(batch.source_run_id),
                source_plan_fingerprint=str(batch.source_plan_fingerprint),
                ordinal=int(item.ordinal),
                content_item_id=int(item.content_item_id),
                captured_content_revision=int(item.captured_content_revision),
                timezone_name=str(batch.timezone),
                local_date_value=item.local_date,
                local_time_value=str(item.local_time),
                scheduled_at=item.resolved_scheduled_at,
            )
            if expected_item_fingerprint != str(item.item_fingerprint):
                return f"item {int(item.ordinal)} fingerprint mismatch"
            expected_item_key = _item_execution_key(
                batch_execution_key=str(batch.execution_key),
                ordinal=int(item.ordinal),
                item_fingerprint=str(item.item_fingerprint),
            )
            if expected_item_key != str(item.execution_key):
                return f"item {int(item.ordinal)} execution key mismatch"
        return None

    async def _item_stale_reason(
        self,
        batch: AdminAgentApprovalBatch,
        item: AdminAgentApprovalBatchItem,
    ) -> str | None:
        artifact = (
            await self.session.execute(
                select(AdminAgentRunArtifact)
                .where(
                    AdminAgentRunArtifact.run_id == int(batch.source_run_id),
                    AdminAgentRunArtifact.artifact_type == SOURCE_ARTIFACT_TYPE,
                    AdminAgentRunArtifact.ordinal == int(item.ordinal),
                    AdminAgentRunArtifact.content_item_id == int(item.content_item_id),
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if artifact is None:
            return "source artifact no longer exists"

        content = (
            await self.session.execute(
                select(ContentItem)
                .where(ContentItem.id == int(item.content_item_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if content is None:
            return "content item was removed"
        if int(content.channel_id) != int(batch.channel_id):
            return "content item channel changed"
        if (
            str(content.kind) != "post"
            or str(content.status) not in _ELIGIBLE_CONTENT_STATUSES
        ):
            return "content item is no longer an eligible draft"
        if int(content.current_revision or 0) != int(item.captured_content_revision):
            return "content revision changed"
        try:
            await self._validate_document(
                content_item_id=int(content.id),
                content_revision=int(item.captured_content_revision),
            )
        except SeriesApprovalInputError:
            return "captured content revision is no longer valid"

        timezone_name = await _resolve_channel_timezone(
            self.session,
            int(batch.channel_id),
        )
        if str(timezone_name) != str(batch.timezone):
            return "channel timezone changed"

        parsed_time = _strict_time(str(item.local_time))
        try:
            recomputed = localize_wall_clock_strict(
                datetime.combine(item.local_date, parsed_time),
                str(batch.timezone),
            ).astimezone(timezone.utc)
        except ValueError as exc:
            return str(exc)
        if recomputed <= self.now_utc:
            return "schedule slot is no longer in the future"
        if as_utc(recomputed) != as_utc(item.resolved_scheduled_at):
            return "resolved schedule timestamp changed"
        if await self._conflicting_schedule_exists(
            content_item_id=int(item.content_item_id),
            content_revision=int(item.captured_content_revision),
        ):
            return "conflicting canonical schedule already exists"
        return None

    async def _full_preflight(
        self,
        batch: AdminAgentApprovalBatch,
        items: list[AdminAgentApprovalBatchItem],
    ) -> tuple[AdminAgentApprovalBatchItem | None, str | None]:
        if len(items) != int(batch.item_count) or [
            int(item.ordinal) for item in items
        ] != list(range(1, int(batch.item_count) + 1)):
            return None, "batch item set changed"
        contract_reason = await self._source_contract_reason(batch, items)
        if contract_reason is not None:
            return None, contract_reason
        timezone_name = await _resolve_channel_timezone(
            self.session,
            int(batch.channel_id),
        )
        if str(timezone_name) != str(batch.timezone):
            return None, "channel timezone changed"
        for item in items:
            if str(item.state) != ITEM_PENDING:
                return item, "batch item is not pending before execution"
            reason = await self._item_stale_reason(batch, item)
            if reason is not None:
                return item, reason
        return None, None

    async def _mark_stale(
        self,
        batch: AdminAgentApprovalBatch,
        *,
        reviewer_tg_user_id: int,
        reason: str,
        item: AdminAgentApprovalBatchItem | None = None,
    ) -> AdminAgentApprovalBatch:
        batch.state = STATE_STALE
        batch.failure_reason = str(reason)
        batch.reviewer_tg_user_id = int(reviewer_tg_user_id)
        batch.reviewed_at = batch.reviewed_at or self.now_utc
        batch.execution_claim_token = None
        batch.execution_claimed_at = None
        if item is not None and str(item.state) in {ITEM_PENDING, ITEM_EXECUTING}:
            item.state = ITEM_STALE
            item.failure_reason = str(reason)
        elif item is None:
            for row in await self.items_for_batch(int(batch.id)):
                if str(row.state) == ITEM_PENDING:
                    row.state = ITEM_STALE
                    row.failure_reason = str(reason)
        await self.session.commit()
        await self.session.refresh(batch)
        return batch

    async def _terminal_failure(
        self,
        batch: AdminAgentApprovalBatch,
        *,
        completed_count: int,
        reason: str,
        failed_item: AdminAgentApprovalBatchItem | None = None,
        stale: bool = False,
    ) -> AdminAgentApprovalBatch:
        if failed_item is not None:
            failed_item.state = ITEM_STALE if stale else ITEM_FAILED
            failed_item.failure_reason = str(reason)
        if completed_count > 0:
            batch.state = STATE_PARTIAL_FAILED
        else:
            batch.state = STATE_STALE if stale else STATE_FAILED
        batch.failure_reason = str(reason)
        batch.execution_claim_token = None
        batch.execution_claimed_at = None
        await self.session.commit()
        await self.session.refresh(batch)
        return batch

    async def _recover_existing(
        self,
        batch: AdminAgentApprovalBatch,
        item: AdminAgentApprovalBatchItem,
    ) -> tuple[str, ScheduleEntry | None, Publication | None]:
        key = str(item.execution_key or "")
        if not key:
            return ("conflict", None, None)
        schedule_candidates = list(
            (
                await self.session.execute(
                    select(ScheduleEntry).where(
                        ScheduleEntry.content_item_id == int(item.content_item_id),
                        ScheduleEntry.content_revision
                        == int(item.captured_content_revision),
                    )
                )
            ).scalars()
        )
        publication_candidates = list(
            (
                await self.session.execute(
                    select(Publication).where(
                        Publication.content_item_id == int(item.content_item_id),
                        Publication.content_revision
                        == int(item.captured_content_revision),
                    )
                )
            ).scalars()
        )
        schedules = [
            row
            for row in schedule_candidates
            if str(dict(row.meta or {}).get("admin_agent_item_execution_key") or "")
            == key
        ]
        publications = [
            row
            for row in publication_candidates
            if str(
                dict(row.meta or {}).get("admin_agent_item_execution_key") or ""
            )
            == key
        ]
        if not schedules and not publications:
            return ("none", None, None)
        if len(schedules) != 1 or len(publications) != 1:
            return ("conflict", None, None)
        schedule = schedules[0]
        publication = publications[0]
        schedule_meta = dict(schedule.meta or {})
        publication_meta = dict(publication.meta or {})
        required = {
            "admin_agent_batch_approval_id": int(batch.id),
            "admin_agent_batch_execution_key": str(batch.execution_key),
            "admin_agent_batch_item_id": int(item.id),
            "admin_agent_item_execution_key": str(item.execution_key),
            "admin_agent_source_run_id": int(batch.source_run_id),
            "admin_agent_series_ordinal": int(item.ordinal),
            "admin_agent_batch_fingerprint": str(batch.action_fingerprint),
            "admin_agent_item_fingerprint": str(item.item_fingerprint),
        }
        if (
            int(publication.schedule_entry_id or 0) != int(schedule.id)
            or int(schedule.channel_id) != int(batch.channel_id)
            or int(publication.channel_id) != int(batch.channel_id)
            or as_utc(schedule.scheduled_at)
            != as_utc(item.resolved_scheduled_at)
            or str(schedule.timezone or "") != str(batch.timezone)
            or str(schedule.status) == "cancelled"
            or any(schedule_meta.get(key_name) != value for key_name, value in required.items())
            or any(
                publication_meta.get(key_name) != value
                for key_name, value in required.items()
            )
        ):
            return ("conflict", None, None)
        return ("match", schedule, publication)

    async def _finalize_item(
        self,
        *,
        batch: AdminAgentApprovalBatch,
        item: AdminAgentApprovalBatchItem,
        schedule: ScheduleEntry,
        publication: Publication,
        claim_token: str,
    ) -> None:
        current = await self.session.get(AdminAgentApprovalBatchItem, int(item.id))
        if current is None:
            raise SeriesApprovalExecutionError("batch item disappeared during execution")
        if str(current.state) == ITEM_EXECUTED:
            return
        if str(current.state) != ITEM_EXECUTING:
            raise SeriesApprovalStateConflict("batch item is no longer executing")

        # Fence the item finalize to the same current batch owner that was allowed
        # to create/recover its canonical pair.
        await self.session.commit()
        if not await fence_execution_claim(
            self.session,
            owner_model=AdminAgentApprovalBatch,
            owner_id=int(batch.id),
            claim_token=claim_token,
        ):
            await self.session.rollback()
            raise SeriesApprovalStateConflict("batch execution claim was lost")

        current.schedule_entry_id = int(schedule.id)
        current.publication_id = int(publication.id)
        current.state = ITEM_EXECUTED
        current.failure_reason = None
        current.executed_at = self.now_utc
        await self.session.commit()
        await self.session.refresh(current)
        item.schedule_entry_id = current.schedule_entry_id
        item.publication_id = current.publication_id
        item.state = current.state
        item.failure_reason = current.failure_reason
        item.executed_at = current.executed_at

    async def _claim_still_owned(
        self,
        *,
        batch_id: int,
        claim_token: str,
    ) -> bool:
        owned_id = (
            await self.session.execute(
                select(AdminAgentApprovalBatch.id).where(
                    AdminAgentApprovalBatch.id == int(batch_id),
                    AdminAgentApprovalBatch.state == STATE_EXECUTING,
                    AdminAgentApprovalBatch.execution_claim_token == str(claim_token),
                )
            )
        ).scalar_one_or_none()
        return owned_id is not None

    async def _queue_item(
        self,
        *,
        batch: AdminAgentApprovalBatch,
        item: AdminAgentApprovalBatchItem,
        claim_token: str,
    ) -> tuple[ScheduleEntry, Publication]:
        # End any read-only validation transaction before the guarded write.
        # queue() commits the canonical pair while this batch-row fence is held.
        await self.session.commit()
        if not await fence_execution_claim(
            self.session,
            owner_model=AdminAgentApprovalBatch,
            owner_id=int(batch.id),
            claim_token=claim_token,
            content_item_id=int(item.content_item_id),
            content_revision=int(item.captured_content_revision),
        ):
            await self.session.rollback()
            raise SeriesApprovalStateConflict("batch execution claim was lost")
        if await self._conflicting_schedule_exists(
            content_item_id=int(item.content_item_id),
            content_revision=int(item.captured_content_revision),
        ):
            raise _CanonicalApprovalTargetStale(
                "conflicting canonical schedule already exists"
            )
        reviewer = int(batch.reviewer_tg_user_id or batch.owner_tg_user_id)
        metadata = {
            "admin_agent_batch_approval_id": int(batch.id),
            "admin_agent_batch_execution_key": str(batch.execution_key),
            "admin_agent_batch_item_id": int(item.id),
            "admin_agent_item_execution_key": str(item.execution_key),
            "admin_agent_source_run_id": int(batch.source_run_id),
            "admin_agent_series_ordinal": int(item.ordinal),
            "admin_agent_owner_tg_user_id": int(batch.owner_tg_user_id),
            "admin_agent_reviewer_tg_user_id": reviewer,
            "admin_agent_action_type": ACTION_SCHEDULE_CONTENT_SERIES,
            "admin_agent_batch_fingerprint": str(batch.action_fingerprint),
            "admin_agent_item_fingerprint": str(item.item_fingerprint),
        }
        publication = await LegacyPublicationBridge(self.session).queue(
            content_item_id=int(item.content_item_id),
            content_revision=int(item.captured_content_revision),
            scheduled_at=item.resolved_scheduled_at,
            timezone_name=str(batch.timezone),
            repeat_rule=None,
            runtime_options=None,
            metadata=metadata,
        )
        schedule_id = int(publication.schedule_entry_id or 0)
        if schedule_id <= 0:
            raise SeriesApprovalExecutionError(
                "canonical schedule result is missing"
            )
        schedule = await self.session.get(ScheduleEntry, schedule_id)
        if schedule is None:
            raise SeriesApprovalExecutionError(
                "canonical schedule result disappeared"
            )
        return schedule, publication

    async def _execute_claimed(
        self,
        batch: AdminAgentApprovalBatch,
        *,
        claim_token: str,
    ) -> AdminAgentApprovalBatch:
        items = await self.items_for_batch(int(batch.id))
        preexisting_executed = sum(
            1 for row in items if str(row.state) == ITEM_EXECUTED
        )
        source_contract_reason = await self._source_contract_reason(batch, items)
        if source_contract_reason is not None:
            return await self._terminal_failure(
                batch,
                completed_count=preexisting_executed,
                reason=source_contract_reason,
                stale=True,
            )
        completed_count = 0
        for item in items:
            source_contract_reason = await self._source_contract_reason(batch, items)
            if source_contract_reason is not None:
                return await self._terminal_failure(
                    batch,
                    completed_count=completed_count,
                    reason=source_contract_reason,
                    stale=True,
                )
            if not await self._claim_still_owned(
                batch_id=int(batch.id),
                claim_token=claim_token,
            ):
                latest = await self._load(
                    batch_id=int(batch.id),
                    owner_tg_user_id=int(batch.owner_tg_user_id),
                    channel_id=int(batch.channel_id),
                )
                if latest is None:
                    raise SeriesApprovalExecutionError(
                        "batch disappeared during execution"
                    )
                return latest

            if str(item.state) == ITEM_EXECUTED:
                recovery, schedule, publication = await self._recover_existing(
                    batch,
                    item,
                )
                if (
                    recovery != "match"
                    or schedule is None
                    or publication is None
                ):
                    return await self._terminal_failure(
                        batch,
                        completed_count=completed_count + 1,
                        reason=(
                            f"item {int(item.ordinal)} canonical recovery conflict"
                        ),
                        failed_item=item,
                    )
                completed_count += 1
                continue

            if str(item.state) in {ITEM_STALE, ITEM_FAILED}:
                return await self._terminal_failure(
                    batch,
                    completed_count=completed_count,
                    reason=str(
                        item.failure_reason
                        or f"item {int(item.ordinal)} cannot continue"
                    ),
                    failed_item=item,
                    stale=str(item.state) == ITEM_STALE,
                )

            if str(item.state) == ITEM_EXECUTING:
                recovery, schedule, publication = await self._recover_existing(
                    batch,
                    item,
                )
                if (
                    recovery == "match"
                    and schedule is not None
                    and publication is not None
                ):
                    await self._finalize_item(
                        batch=batch,
                        item=item,
                        schedule=schedule,
                        publication=publication,
                        claim_token=claim_token,
                    )
                    completed_count += 1
                    continue
                if recovery == "conflict":
                    return await self._terminal_failure(
                        batch,
                        completed_count=completed_count,
                        reason=(
                            f"item {int(item.ordinal)} canonical recovery conflict"
                        ),
                        failed_item=item,
                    )
                reason = await self._item_stale_reason(batch, item)
                if reason is not None:
                    return await self._terminal_failure(
                        batch,
                        completed_count=completed_count,
                        reason=f"item {int(item.ordinal)}: {reason}",
                        failed_item=item,
                        stale=True,
                    )
            elif str(item.state) == ITEM_PENDING:
                reason = await self._item_stale_reason(batch, item)
                if reason is not None:
                    return await self._terminal_failure(
                        batch,
                        completed_count=completed_count,
                        reason=f"item {int(item.ordinal)}: {reason}",
                        failed_item=item,
                        stale=True,
                    )
                item.state = ITEM_EXECUTING
                item.execution_started_at = self.now_utc
                item.failure_reason = None
                await self.session.commit()
                await self.session.refresh(item)
            else:
                return await self._terminal_failure(
                    batch,
                    completed_count=completed_count,
                    reason=f"item {int(item.ordinal)} has invalid state",
                    failed_item=item,
                )

            item_id = int(item.id)
            batch_id = int(batch.id)
            try:
                schedule, publication = await self._queue_item(
                    batch=batch,
                    item=item,
                    claim_token=claim_token,
                )
                await self._finalize_item(
                    batch=batch,
                    item=item,
                    schedule=schedule,
                    publication=publication,
                    claim_token=claim_token,
                )
                completed_count += 1
            except _CanonicalApprovalTargetStale as exc:
                return await self._terminal_failure(
                    batch,
                    completed_count=completed_count,
                    reason=f"item {int(item.ordinal)}: {exc}",
                    failed_item=item,
                    stale=True,
                )
            except (SeriesApprovalExecutionError, SeriesApprovalStateConflict):
                await self.session.rollback()
                raise
            except Exception as exc:
                await self.session.rollback()
                current_item = await self.session.get(
                    AdminAgentApprovalBatchItem,
                    item_id,
                )
                current_batch = await self.session.get(
                    AdminAgentApprovalBatch,
                    batch_id,
                )
                if current_item is not None and current_batch is not None:
                    recovery, schedule, publication = await self._recover_existing(
                        current_batch,
                        current_item,
                    )
                    if (
                        recovery == "match"
                        and schedule is not None
                        and publication is not None
                    ):
                        await self._finalize_item(
                            batch=current_batch,
                            item=current_item,
                            schedule=schedule,
                            publication=publication,
                            claim_token=claim_token,
                        )
                        completed_count += 1
                        continue
                    if recovery == "conflict":
                        return await self._terminal_failure(
                            current_batch,
                            completed_count=completed_count,
                            reason=(
                                f"item {int(current_item.ordinal)} canonical "
                                "recovery conflict"
                            ),
                            failed_item=current_item,
                        )
                    current_batch.failure_reason = (
                        f"item {int(current_item.ordinal)} canonical scheduling "
                        "outcome unknown; retry approval to recover"
                    )
                    await self.session.commit()
                raise SeriesApprovalExecutionError(
                    "canonical series scheduling attempt failed"
                ) from exc

        # The final batch state is canonical execution bookkeeping too. Fence it
        # so an executor that lost the lease after its last item commit cannot
        # clear the takeover owner's claim or mark the batch executed.
        await self.session.commit()
        if not await fence_execution_claim(
            self.session,
            owner_model=AdminAgentApprovalBatch,
            owner_id=int(batch.id),
            claim_token=claim_token,
        ):
            await self.session.rollback()
            latest = await self._load(
                batch_id=int(batch.id),
                owner_tg_user_id=int(batch.owner_tg_user_id),
                channel_id=int(batch.channel_id),
            )
            if latest is None:
                raise SeriesApprovalExecutionError(
                    "batch disappeared during execution"
                )
            return latest

        current = await self._load(
            batch_id=int(batch.id),
            owner_tg_user_id=int(batch.owner_tg_user_id),
            channel_id=int(batch.channel_id),
            for_update=True,
        )
        if current is None:
            raise SeriesApprovalExecutionError("batch disappeared during execution")
        if str(current.state) == STATE_EXECUTED:
            return current
        if str(current.state) != STATE_EXECUTING:
            return current
        current.state = STATE_EXECUTED
        current.failure_reason = None
        current.executed_at = self.now_utc
        current.execution_claim_token = None
        current.execution_claimed_at = None
        await self.session.commit()
        await self.session.refresh(current)
        return current

    async def approve(
        self,
        *,
        batch_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        reviewer_tg_user_id: int,
    ) -> AdminAgentApprovalBatch | None:
        batch = await self._load(
            batch_id=batch_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            for_update=True,
        )
        if batch is None:
            return None
        if str(batch.state) in {
            STATE_EXECUTED,
            STATE_REJECTED,
            STATE_STALE,
            STATE_PARTIAL_FAILED,
            STATE_FAILED,
        }:
            return batch

        if str(batch.state) == STATE_EXECUTING:
            claimed_at = (
                as_utc(batch.execution_claimed_at)
                if batch.execution_claimed_at is not None
                else None
            )
            if (
                claimed_at is not None
                and (self.now_utc - claimed_at).total_seconds()
                < _EXECUTION_CLAIM_SECONDS
                and batch.execution_claim_token
            ):
                return batch
            old_token = batch.execution_claim_token
            new_token = _claim_token()
            await self.session.commit()
            token_clause = (
                AdminAgentApprovalBatch.execution_claim_token == str(old_token)
                if old_token is not None
                else AdminAgentApprovalBatch.execution_claim_token.is_(None)
            )
            transition = await self.session.execute(
                update(AdminAgentApprovalBatch)
                .where(
                    AdminAgentApprovalBatch.id == int(batch_id),
                    AdminAgentApprovalBatch.owner_tg_user_id
                    == int(owner_tg_user_id),
                    AdminAgentApprovalBatch.channel_id == int(channel_id),
                    AdminAgentApprovalBatch.state == STATE_EXECUTING,
                    token_clause,
                )
                .values(
                    execution_claim_token=new_token,
                    execution_claimed_at=self.now_utc,
                    reviewer_tg_user_id=int(reviewer_tg_user_id),
                )
            )
            await self.session.commit()
            if int(transition.rowcount or 0) != 1:
                return await self._load(
                    batch_id=batch_id,
                    owner_tg_user_id=owner_tg_user_id,
                    channel_id=channel_id,
                )
            claimed = await self._load(
                batch_id=batch_id,
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
            )
            if claimed is None:
                raise SeriesApprovalExecutionError(
                    "batch disappeared after recovery claim"
                )
            return await self._execute_claimed(
                claimed,
                claim_token=new_token,
            )

        if str(batch.state) != STATE_PENDING_REVIEW:
            return batch

        items = await self.items_for_batch(int(batch.id))
        stale_item, stale_reason = await self._full_preflight(batch, items)
        if stale_reason is not None:
            return await self._mark_stale(
                batch,
                reviewer_tg_user_id=reviewer_tg_user_id,
                reason=stale_reason,
                item=stale_item,
            )

        claim_token = _claim_token()
        await self.session.commit()
        transition = await self.session.execute(
            update(AdminAgentApprovalBatch)
            .where(
                AdminAgentApprovalBatch.id == int(batch_id),
                AdminAgentApprovalBatch.owner_tg_user_id == int(owner_tg_user_id),
                AdminAgentApprovalBatch.channel_id == int(channel_id),
                AdminAgentApprovalBatch.state == STATE_PENDING_REVIEW,
            )
            .values(
                state=STATE_EXECUTING,
                execution_claim_token=claim_token,
                execution_claimed_at=self.now_utc,
                reviewer_tg_user_id=int(reviewer_tg_user_id),
                reviewed_at=self.now_utc,
                failure_reason=None,
            )
        )
        await self.session.commit()
        if int(transition.rowcount or 0) != 1:
            return await self.approve(
                batch_id=batch_id,
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
                reviewer_tg_user_id=reviewer_tg_user_id,
            )

        claimed = await self._load(
            batch_id=batch_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
        )
        if claimed is None:
            raise SeriesApprovalExecutionError(
                "batch disappeared after execution claim"
            )
        claimed_items = await self.items_for_batch(int(claimed.id))
        stale_item, stale_reason = await self._full_preflight(
            claimed,
            claimed_items,
        )
        if stale_reason is not None:
            return await self._mark_stale(
                claimed,
                reviewer_tg_user_id=reviewer_tg_user_id,
                reason=stale_reason,
                item=stale_item,
            )
        return await self._execute_claimed(
            claimed,
            claim_token=claim_token,
        )

    async def reject(
        self,
        *,
        batch_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        reviewer_tg_user_id: int,
    ) -> AdminAgentApprovalBatch | None:
        batch = await self._load(
            batch_id=batch_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            for_update=True,
        )
        if batch is None:
            return None
        if str(batch.state) == STATE_REJECTED:
            return batch
        if str(batch.state) != STATE_PENDING_REVIEW:
            raise SeriesApprovalStateConflict(
                "series approval can no longer be rejected"
            )
        batch.state = STATE_REJECTED
        batch.reviewer_tg_user_id = int(reviewer_tg_user_id)
        batch.reviewed_at = self.now_utc
        batch.failure_reason = None
        batch.execution_claim_token = None
        batch.execution_claimed_at = None
        await self.session.commit()
        await self.session.refresh(batch)
        return batch
