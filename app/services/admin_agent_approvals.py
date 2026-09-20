from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import localize_wall_clock_strict, to_user_tz
from app.domain.admin_agent import AdminAgentApproval, AdminAgentRun
from app.domain.content import PostDocument, validate_native_document_capabilities
from app.domain.content.models import ContentItem, ContentRevision
from app.domain.publishing.models import Publication, ScheduleEntry
from app.services.admin_agent import SCENARIO_DRAFTS_TOMORROW, _resolve_channel_timezone
from app.services.admin_agent_execution_fence import fence_execution_claim
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.scheduling import as_utc


ACTION_SCHEDULE_DRAFT_TOMORROW = "schedule_draft_tomorrow"

STATE_PENDING_REVIEW = "pending_review"
STATE_EXECUTING = "executing"
STATE_EXECUTED = "executed"
STATE_REJECTED = "rejected"
STATE_STALE = "stale"
STATE_FAILED = "failed"

_EXECUTION_CLAIM_SECONDS = 30
_ELIGIBLE_CONTENT_STATUSES = frozenset({"draft"})


class ApprovalInputError(ValueError):
    pass


class ApprovalStateConflict(RuntimeError):
    pass


class ApprovalExecutionError(RuntimeError):
    pass


def _normalized_now(value: datetime | None = None) -> datetime:
    return as_utc(value or datetime.now(timezone.utc))


def _parse_local_time(value: str) -> time:
    raw = str(value or "").strip()
    if len(raw) != 5:
        raise ApprovalInputError("local_time must use HH:MM")
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ApprovalInputError("local_time must use HH:MM") from exc
    if parsed.strftime("%H:%M") != raw:
        raise ApprovalInputError("local_time must use HH:MM")
    return parsed


def _fingerprint_payload(
    *,
    owner_tg_user_id: int,
    channel_id: int,
    content_item_id: int,
    content_revision: int,
    timezone_name: str,
    target_local_date,
    local_time_value: str,
    scheduled_at: datetime,
) -> dict[str, object]:
    return {
        "action_type": ACTION_SCHEDULE_DRAFT_TOMORROW,
        "owner_tg_user_id": int(owner_tg_user_id),
        "channel_id": int(channel_id),
        "content_item_id": int(content_item_id),
        "content_revision": int(content_revision),
        "timezone": str(timezone_name),
        "target_local_date": target_local_date.isoformat(),
        "local_time": str(local_time_value),
        "scheduled_at_utc": as_utc(scheduled_at).isoformat(),
    }


def _fingerprint(**kwargs) -> str:
    canonical = json.dumps(
        _fingerprint_payload(**kwargs),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _execution_key(approval: AdminAgentApproval) -> str:
    raw = (
        f"admin-agent-approval:{int(approval.id)}:"
        f"{str(approval.action_fingerprint)}"
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _claim_token() -> str:
    return sha256(uuid4().hex.encode("ascii")).hexdigest()


class AdminAgentApprovalService:
    """Deterministic approval boundary for the single MVP-C mutation."""

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
        approval_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        for_update: bool = False,
    ) -> AdminAgentApproval | None:
        stmt = select(AdminAgentApproval).where(
            AdminAgentApproval.id == int(approval_id),
            AdminAgentApproval.owner_tg_user_id == int(owner_tg_user_id),
            AdminAgentApproval.channel_id == int(channel_id),
        )
        stmt = stmt.execution_options(populate_existing=True)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get(
        self,
        *,
        approval_id: int,
        owner_tg_user_id: int,
        channel_id: int,
    ) -> AdminAgentApproval | None:
        return await self._load(
            approval_id=approval_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
        )

    async def list(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        limit: int = 50,
    ) -> list[AdminAgentApproval]:
        return list(
            (
                await self.session.execute(
                    select(AdminAgentApproval)
                    .where(
                        AdminAgentApproval.owner_tg_user_id == int(owner_tg_user_id),
                        AdminAgentApproval.channel_id == int(channel_id),
                    )
                    .order_by(AdminAgentApproval.id.desc())
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
        limit: int = 8,
    ) -> list[AdminAgentApproval]:
        latest_ids = (
            select(func.max(AdminAgentApproval.id).label("approval_id"))
            .where(
                AdminAgentApproval.owner_tg_user_id == int(owner_tg_user_id),
                AdminAgentApproval.channel_id == int(channel_id),
                AdminAgentApproval.source_admin_agent_run_id == int(source_run_id),
                AdminAgentApproval.action_type == ACTION_SCHEDULE_DRAFT_TOMORROW,
            )
            .group_by(AdminAgentApproval.content_item_id)
            .subquery()
        )
        return list(
            (
                await self.session.execute(
                    select(AdminAgentApproval)
                    .join(
                        latest_ids,
                        AdminAgentApproval.id == latest_ids.c.approval_id,
                    )
                    .order_by(AdminAgentApproval.id.desc())
                    .limit(max(1, min(int(limit), 8)))
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
            raise ApprovalInputError("content revision not found")
        try:
            document = PostDocument.from_dict(revision.document)
            document.validate()
            validate_native_document_capabilities(document)
        except Exception as exc:
            raise ApprovalInputError("content document is invalid") from exc

    async def _source_run_id(
        self,
        *,
        item: ContentItem,
        owner_tg_user_id: int,
        channel_id: int,
    ) -> int | None:
        raw = dict(item.meta or {}).get("admin_agent_run_id")
        try:
            run_id = int(raw)
        except (TypeError, ValueError):
            return None
        run = await self.session.get(AdminAgentRun, run_id)
        if (
            run is None
            or int(run.owner_tg_user_id) != int(owner_tg_user_id)
            or int(run.channel_id) != int(channel_id)
            or str(run.scenario) != SCENARIO_DRAFTS_TOMORROW
        ):
            return None
        return int(run.id)

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

    async def create_schedule_draft_tomorrow(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        content_item_id: int,
        local_time_value: str,
        request_id: str,
    ) -> AdminAgentApproval:
        existing = (
            await self.session.execute(
                select(AdminAgentApproval).where(
                    AdminAgentApproval.owner_tg_user_id == int(owner_tg_user_id),
                    AdminAgentApproval.channel_id == int(channel_id),
                    AdminAgentApproval.action_type == ACTION_SCHEDULE_DRAFT_TOMORROW,
                    AdminAgentApproval.request_id == str(request_id),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        parsed_time = _parse_local_time(local_time_value)
        item = await self.session.get(ContentItem, int(content_item_id))
        if item is None or int(item.channel_id) != int(channel_id):
            raise ApprovalInputError("draft not found")
        if str(item.kind) != "post" or str(item.status) not in _ELIGIBLE_CONTENT_STATUSES:
            raise ApprovalInputError("content item is not an eligible draft")
        revision = int(item.current_revision or 0)
        if revision <= 0:
            raise ApprovalInputError("draft has no current revision")
        await self._validate_document(
            content_item_id=int(item.id),
            content_revision=revision,
        )
        if await self._conflicting_schedule_exists(
            content_item_id=int(item.id),
            content_revision=revision,
        ):
            raise ApprovalInputError("draft already has canonical scheduling state")

        timezone_name = await _resolve_channel_timezone(self.session, int(channel_id))
        local_now = to_user_tz(self.now_utc, timezone_name)
        target_date = local_now.date() + timedelta(days=1)
        local_naive = datetime.combine(target_date, parsed_time)
        try:
            scheduled_at = localize_wall_clock_strict(
                local_naive,
                timezone_name,
            ).astimezone(timezone.utc)
        except ValueError as exc:
            raise ApprovalInputError(str(exc)) from exc
        if scheduled_at <= self.now_utc:
            raise ApprovalInputError("target schedule time is not in the future")

        fingerprint = _fingerprint(
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            content_item_id=int(item.id),
            content_revision=revision,
            timezone_name=timezone_name,
            target_local_date=target_date,
            local_time_value=parsed_time.strftime("%H:%M"),
            scheduled_at=scheduled_at,
        )
        source_run_id = await self._source_run_id(
            item=item,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
        )
        # End the validation-only transaction before the insert. On SQLite this
        # avoids two concurrent readers contending while upgrading to writers;
        # the partial unique index remains the durable race authority.
        await self.session.commit()
        approval = AdminAgentApproval(
            owner_tg_user_id=int(owner_tg_user_id),
            channel_id=int(channel_id),
            source_admin_agent_run_id=source_run_id,
            action_type=ACTION_SCHEDULE_DRAFT_TOMORROW,
            state=STATE_PENDING_REVIEW,
            content_item_id=int(item.id),
            content_revision=revision,
            timezone=str(timezone_name),
            target_local_date=target_date,
            local_time=parsed_time.strftime("%H:%M"),
            resolved_scheduled_at=scheduled_at,
            action_fingerprint=fingerprint,
            execution_key=None,
            request_id=str(request_id),
            schedule_entry_id=None,
            publication_id=None,
            reviewer_tg_user_id=None,
            failure_reason=None,
            execution_claim_token=None,
            execution_claimed_at=None,
            reviewed_at=None,
            executed_at=None,
        )
        self.session.add(approval)
        try:
            await self.session.commit()
            await self.session.refresh(approval)
            return approval
        except IntegrityError:
            await self.session.rollback()
            retry = (
                await self.session.execute(
                    select(AdminAgentApproval).where(
                        AdminAgentApproval.owner_tg_user_id == int(owner_tg_user_id),
                        AdminAgentApproval.channel_id == int(channel_id),
                        AdminAgentApproval.action_type == ACTION_SCHEDULE_DRAFT_TOMORROW,
                        AdminAgentApproval.request_id == str(request_id),
                    )
                )
            ).scalar_one_or_none()
            if retry is not None:
                return retry
            active = (
                await self.session.execute(
                    select(AdminAgentApproval).where(
                        AdminAgentApproval.owner_tg_user_id == int(owner_tg_user_id),
                        AdminAgentApproval.channel_id == int(channel_id),
                        AdminAgentApproval.action_type
                        == ACTION_SCHEDULE_DRAFT_TOMORROW,
                        AdminAgentApproval.content_item_id == int(content_item_id),
                        AdminAgentApproval.content_revision == int(revision),
                        AdminAgentApproval.state.in_(
                            [STATE_PENDING_REVIEW, STATE_EXECUTING]
                        ),
                    )
                )
            ).scalar_one_or_none()
            if active is not None:
                raise ApprovalStateConflict(
                    "an active approval already exists for this draft revision"
                )
            raise

    async def _stale_reason(self, approval: AdminAgentApproval) -> str | None:
        item = (
            await self.session.execute(
                select(ContentItem)
                .where(ContentItem.id == int(approval.content_item_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if item is None:
            return "draft was removed"
        if int(item.channel_id) != int(approval.channel_id):
            return "draft channel changed"
        if str(item.kind) != "post" or str(item.status) not in _ELIGIBLE_CONTENT_STATUSES:
            return "draft is no longer eligible"
        if int(item.current_revision or 0) != int(approval.content_revision):
            return "draft revision changed"
        try:
            await self._validate_document(
                content_item_id=int(item.id),
                content_revision=int(approval.content_revision),
            )
        except ApprovalInputError:
            return "captured draft revision is no longer valid"

        timezone_name = await _resolve_channel_timezone(
            self.session,
            int(approval.channel_id),
        )
        if str(timezone_name) != str(approval.timezone):
            return "channel timezone changed"

        parsed_time = _parse_local_time(str(approval.local_time))
        current_tomorrow = to_user_tz(self.now_utc, timezone_name).date() + timedelta(days=1)
        if current_tomorrow != approval.target_local_date:
            return "proposal no longer targets tomorrow in channel timezone"
        try:
            recomputed = localize_wall_clock_strict(
                datetime.combine(approval.target_local_date, parsed_time),
                timezone_name,
            ).astimezone(timezone.utc)
        except ValueError as exc:
            return str(exc)
        if recomputed <= self.now_utc:
            return "target schedule time is no longer in the future"
        if as_utc(approval.resolved_scheduled_at) != as_utc(recomputed):
            return "resolved target time changed"

        fingerprint = _fingerprint(
            owner_tg_user_id=int(approval.owner_tg_user_id),
            channel_id=int(approval.channel_id),
            content_item_id=int(approval.content_item_id),
            content_revision=int(approval.content_revision),
            timezone_name=str(approval.timezone),
            target_local_date=approval.target_local_date,
            local_time_value=str(approval.local_time),
            scheduled_at=approval.resolved_scheduled_at,
        )
        if fingerprint != str(approval.action_fingerprint):
            return "approval fingerprint mismatch"
        if await self._conflicting_schedule_exists(
            content_item_id=int(approval.content_item_id),
            content_revision=int(approval.content_revision),
        ):
            return "canonical scheduling state already exists"
        return None

    async def _mark_stale(
        self,
        approval: AdminAgentApproval,
        *,
        reviewer_tg_user_id: int,
        reason: str,
    ) -> AdminAgentApproval:
        approval.state = STATE_STALE
        approval.failure_reason = str(reason)
        approval.reviewer_tg_user_id = int(reviewer_tg_user_id)
        approval.reviewed_at = self.now_utc
        approval.execution_claim_token = None
        approval.execution_claimed_at = None
        await self.session.commit()
        await self.session.refresh(approval)
        return approval

    async def _mark_failed(
        self,
        approval: AdminAgentApproval,
        *,
        reason: str,
    ) -> AdminAgentApproval:
        approval.state = STATE_FAILED
        approval.failure_reason = str(reason)
        approval.execution_claim_token = None
        approval.execution_claimed_at = None
        await self.session.commit()
        await self.session.refresh(approval)
        return approval

    async def _recover_existing(
        self,
        approval: AdminAgentApproval,
    ) -> tuple[str, ScheduleEntry | None, Publication | None]:
        key = str(approval.execution_key or "")
        if not key:
            return ("none", None, None)

        schedule_candidates = list(
            (
                await self.session.execute(
                    select(ScheduleEntry).where(
                        ScheduleEntry.content_item_id == int(approval.content_item_id),
                        ScheduleEntry.content_revision == int(approval.content_revision),
                    )
                )
            ).scalars()
        )
        publication_candidates = list(
            (
                await self.session.execute(
                    select(Publication).where(
                        Publication.content_item_id == int(approval.content_item_id),
                        Publication.content_revision == int(approval.content_revision),
                    )
                )
            ).scalars()
        )
        schedules = [
            row
            for row in schedule_candidates
            if str(dict(row.meta or {}).get("admin_agent_execution_key") or "") == key
        ]
        publications = [
            row
            for row in publication_candidates
            if str(dict(row.meta or {}).get("admin_agent_execution_key") or "") == key
        ]
        if not schedules and not publications:
            return ("none", None, None)
        if len(schedules) != 1 or len(publications) != 1:
            return ("conflict", None, None)
        schedule = schedules[0]
        publication = publications[0]
        if (
            int(publication.schedule_entry_id or 0) != int(schedule.id)
            or int(schedule.channel_id) != int(approval.channel_id)
            or int(publication.channel_id) != int(approval.channel_id)
            or as_utc(schedule.scheduled_at) != as_utc(approval.resolved_scheduled_at)
            or int(dict(schedule.meta or {}).get("admin_agent_approval_id") or 0)
            != int(approval.id)
            or int(dict(publication.meta or {}).get("admin_agent_approval_id") or 0)
            != int(approval.id)
        ):
            return ("conflict", None, None)
        return ("match", schedule, publication)

    async def _finalize_executed(
        self,
        *,
        approval_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        claim_token: str,
        schedule_entry_id: int,
        publication_id: int,
    ) -> AdminAgentApproval:
        # Recovery reads may have opened a SQLite read transaction. End it before
        # the guarded write so this fence is the first statement in the commit
        # transaction that records the terminal approval state.
        await self.session.commit()
        if not await fence_execution_claim(
            self.session,
            owner_model=AdminAgentApproval,
            owner_id=int(approval_id),
            claim_token=claim_token,
        ):
            await self.session.rollback()
            current = await self._load(
                approval_id=int(approval_id),
                owner_tg_user_id=int(owner_tg_user_id),
                channel_id=int(channel_id),
            )
            if current is None:
                raise ApprovalExecutionError("approval disappeared during execution")
            if current.state == STATE_EXECUTED:
                return current
            raise ApprovalStateConflict("approval execution claim was lost")

        current = await self._load(
            approval_id=int(approval_id),
            owner_tg_user_id=int(owner_tg_user_id),
            channel_id=int(channel_id),
            for_update=True,
        )
        if current is None:
            raise ApprovalExecutionError("approval disappeared during execution")
        if current.state == STATE_EXECUTED:
            return current
        if current.state != STATE_EXECUTING:
            raise ApprovalStateConflict("approval is no longer executing")
        current.schedule_entry_id = int(schedule_entry_id)
        current.publication_id = int(publication_id)
        current.state = STATE_EXECUTED
        current.executed_at = self.now_utc
        current.failure_reason = None
        current.execution_claim_token = None
        current.execution_claimed_at = None
        await self.session.commit()
        await self.session.refresh(current)
        return current

    async def _execute_claimed(
        self,
        approval: AdminAgentApproval,
        *,
        claim_token: str,
    ) -> AdminAgentApproval:
        approval_id = int(approval.id)
        owner_tg_user_id = int(approval.owner_tg_user_id)
        channel_id = int(approval.channel_id)
        metadata = {
            "admin_agent_approval_id": approval_id,
            "admin_agent_execution_key": str(approval.execution_key),
            "admin_agent_action_type": ACTION_SCHEDULE_DRAFT_TOMORROW,
            "admin_agent_owner_tg_user_id": owner_tg_user_id,
            "admin_agent_reviewer_tg_user_id": int(
                approval.reviewer_tg_user_id or approval.owner_tg_user_id
            ),
            "admin_agent_fingerprint": str(approval.action_fingerprint),
        }

        # Keep the claim fence and canonical queue commit in one transaction.
        # A takeover that commits first makes this guarded write affect zero rows;
        # a fence that wins first prevents takeover until queue() commits.
        await self.session.commit()
        if not await fence_execution_claim(
            self.session,
            owner_model=AdminAgentApproval,
            owner_id=approval_id,
            claim_token=claim_token,
            content_item_id=int(approval.content_item_id),
            content_revision=int(approval.content_revision),
        ):
            await self.session.rollback()
            current = await self._load(
                approval_id=approval_id,
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
            )
            if current is None:
                raise ApprovalExecutionError("approval disappeared during execution")
            return current

        # The ContentRevision fence above is shared with series execution. Recheck
        # canonical state only after that durable target authority is held; this
        # closes the cross-mode TOCTOU between preflight and queue().
        if await self._conflicting_schedule_exists(
            content_item_id=int(approval.content_item_id),
            content_revision=int(approval.content_revision),
        ):
            current = await self._load(
                approval_id=approval_id,
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
                for_update=True,
            )
            if current is None:
                raise ApprovalExecutionError("approval disappeared during execution")
            return await self._mark_stale(
                current,
                reviewer_tg_user_id=int(
                    current.reviewer_tg_user_id or current.owner_tg_user_id
                ),
                reason="canonical scheduling state already exists",
            )

        try:
            publication = await LegacyPublicationBridge(self.session).queue(
                content_item_id=int(approval.content_item_id),
                content_revision=int(approval.content_revision),
                scheduled_at=approval.resolved_scheduled_at,
                timezone_name=str(approval.timezone),
                repeat_rule=None,
                runtime_options=None,
                metadata=metadata,
            )
            schedule_id = int(publication.schedule_entry_id or 0)
            if schedule_id <= 0:
                raise ApprovalExecutionError("canonical schedule result is missing")
            return await self._finalize_executed(
                approval_id=approval_id,
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
                claim_token=claim_token,
                schedule_entry_id=schedule_id,
                publication_id=int(publication.id),
            )
        except Exception as exc:
            await self.session.rollback()
            current = await self._load(
                approval_id=approval_id,
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
            )
            if (
                current is not None
                and current.state == STATE_EXECUTING
                and str(current.execution_claim_token or "") == str(claim_token)
            ):
                current.failure_reason = (
                    "canonical scheduling outcome unknown; retry approval to recover"
                )
                await self.session.commit()
            if isinstance(exc, (ApprovalExecutionError, ApprovalStateConflict)):
                raise
            raise ApprovalExecutionError("canonical scheduling attempt failed") from exc

    async def approve(
        self,
        *,
        approval_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        reviewer_tg_user_id: int,
    ) -> AdminAgentApproval | None:
        approval = await self._load(
            approval_id=approval_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            for_update=True,
        )
        if approval is None:
            return None
        if approval.state == STATE_EXECUTED:
            return approval
        if approval.state in {STATE_REJECTED, STATE_STALE, STATE_FAILED}:
            return approval

        if approval.state == STATE_EXECUTING:
            recovery, schedule, publication = await self._recover_existing(approval)
            if recovery == "match" and schedule is not None and publication is not None:
                return await self._finalize_executed(
                    approval_id=int(approval.id),
                    owner_tg_user_id=int(approval.owner_tg_user_id),
                    channel_id=int(approval.channel_id),
                    claim_token=str(approval.execution_claim_token or ""),
                    schedule_entry_id=int(schedule.id),
                    publication_id=int(publication.id),
                )
            if recovery == "conflict":
                return await self._mark_failed(
                    approval,
                    reason="conflicting canonical results found for execution key",
                )
            claimed_at = (
                as_utc(approval.execution_claimed_at)
                if approval.execution_claimed_at is not None
                else None
            )
            if (
                claimed_at is not None
                and (self.now_utc - claimed_at).total_seconds() < _EXECUTION_CLAIM_SECONDS
                and approval.execution_claim_token
            ):
                return approval
            stale_reason = await self._stale_reason(approval)
            if stale_reason is not None:
                return await self._mark_stale(
                    approval,
                    reviewer_tg_user_id=reviewer_tg_user_id,
                    reason=stale_reason,
                )
            claim_token = _claim_token()
            approval.execution_claim_token = claim_token
            approval.execution_claimed_at = self.now_utc
            approval.reviewer_tg_user_id = int(reviewer_tg_user_id)
            await self.session.commit()
            await self.session.refresh(approval)
            return await self._execute_claimed(
                approval,
                claim_token=claim_token,
            )

        if approval.state != STATE_PENDING_REVIEW:
            return approval

        stale_reason = await self._stale_reason(approval)
        if stale_reason is not None:
            return await self._mark_stale(
                approval,
                reviewer_tg_user_id=reviewer_tg_user_id,
                reason=stale_reason,
            )

        execution_key = approval.execution_key or _execution_key(approval)
        claim_token = _claim_token()
        # End the read-only transaction before the compare-and-set. This matters
        # on SQLite, where two concurrent readers upgrading to writers can contend.
        # There are no staged writes here, and project sessions use
        # expire_on_commit=False, so request-scoped ORM identity remains usable.
        await self.session.commit()
        transition = await self.session.execute(
            update(AdminAgentApproval)
            .where(
                AdminAgentApproval.id == int(approval_id),
                AdminAgentApproval.owner_tg_user_id == int(owner_tg_user_id),
                AdminAgentApproval.channel_id == int(channel_id),
                AdminAgentApproval.state == STATE_PENDING_REVIEW,
            )
            .values(
                state=STATE_EXECUTING,
                execution_key=execution_key,
                execution_claim_token=claim_token,
                execution_claimed_at=self.now_utc,
                reviewer_tg_user_id=int(reviewer_tg_user_id),
                reviewed_at=self.now_utc,
                failure_reason=None,
            )
        )
        await self.session.commit()
        if int(transition.rowcount or 0) != 1:
            # Another approver won the durable transition. Re-enter through the
            # executing recovery path; a fresh claim prevents a second queue call.
            return await self.approve(
                approval_id=approval_id,
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
                reviewer_tg_user_id=reviewer_tg_user_id,
            )

        claimed = await self._load(
            approval_id=approval_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
        )
        if claimed is None:
            raise ApprovalExecutionError("approval disappeared after execution claim")

        # Revalidate once more after the claim commit so edits or a competing
        # canonical schedule that landed during review cannot be silently ignored.
        stale_after_claim = await self._stale_reason(claimed)
        if stale_after_claim is not None:
            return await self._mark_stale(
                claimed,
                reviewer_tg_user_id=reviewer_tg_user_id,
                reason=stale_after_claim,
            )
        return await self._execute_claimed(
            claimed,
            claim_token=claim_token,
        )

    async def reject(
        self,
        *,
        approval_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        reviewer_tg_user_id: int,
    ) -> AdminAgentApproval | None:
        approval = await self._load(
            approval_id=approval_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            for_update=True,
        )
        if approval is None:
            return None
        if approval.state in {STATE_REJECTED, STATE_STALE, STATE_FAILED}:
            return approval
        if approval.state != STATE_PENDING_REVIEW:
            raise ApprovalStateConflict("approval can no longer be rejected")
        approval.state = STATE_REJECTED
        approval.reviewer_tg_user_id = int(reviewer_tg_user_id)
        approval.reviewed_at = self.now_utc
        approval.failure_reason = None
        approval.execution_claim_token = None
        approval.execution_claimed_at = None
        await self.session.commit()
        await self.session.refresh(approval)
        return approval
