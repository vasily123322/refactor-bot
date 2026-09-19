from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import localize_dt, to_user_tz
from app.domain.admin_agent import AdminAgentAutomation, AdminAgentRun
from app.domain.models import Channel, Client
from app.services.admin_agent import (
    AdminAgentRunner,
    _normalize_content_series_input,
    _resolve_channel_timezone,
    assistant_run_resume_state,
)
from app.services.admin_agent_skills import (
    AUTOMATION_BOUNDED,
    APPROVAL_NONE,
    CAPABILITY_DRAFT_WRITE,
    CAPABILITY_READ_ONLY,
    AdminAgentSkillSpec,
    SKILL_REGISTRY,
)
from app.services.scheduling import as_utc


CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
MISFIRE_GRACE = timedelta(minutes=15)
CLAIM_LEASE = timedelta(minutes=2)
MAX_OCCURRENCES_PER_TICK = 5

DISABLED_MANUAL_PAUSE = "manual_pause"
DISABLED_OWNERSHIP_LOST = "ownership_lost"
DISABLED_UNSUPPORTED_SKILL_VERSION = "unsupported_skill_version"
DISABLED_INVALID_DEFINITION = "invalid_definition"
SAFETY_DISABLED_REASONS = {
    DISABLED_OWNERSHIP_LOST,
    DISABLED_UNSUPPORTED_SKILL_VERSION,
    DISABLED_INVALID_DEFINITION,
}

OUTCOME_RUN_RECORDED = "run_recorded"
OUTCOME_MISFIRE_SKIPPED = "misfire_skipped"
OUTCOME_SAFETY_DISABLED = "safety_disabled"

HEALTH_ACTIVE = "active"
HEALTH_PAUSED = "paused"
HEALTH_BLOCKED = "blocked"
HEALTH_NEEDS_ATTENTION = "needs_attention"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_LOCAL_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AutomationInputError(ValueError):
    pass


class AutomationIdempotencyConflict(RuntimeError):
    pass


class AutomationControlConflict(RuntimeError):
    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


def _ensure_automation_skill(spec: AdminAgentSkillSpec) -> None:
    if spec.automation_policy != AUTOMATION_BOUNDED:
        raise AutomationInputError("skill version is not enabled for automation")
    capabilities = set(spec.allowed_capability_classes)
    if (
        not capabilities
        or not capabilities <= {CAPABILITY_READ_ONLY, CAPABILITY_DRAFT_WRITE}
        or spec.approval_requirement != APPROVAL_NONE
    ):
        raise AutomationInputError("skill automation policy is not safe")


def normalize_automation_operator_input(
    spec: AdminAgentSkillSpec,
    value: Mapping[str, object] | None,
) -> dict:
    raw = dict(value or {})
    if spec.scenario in {"attention_today", "drafts_tomorrow"}:
        if raw:
            raise AutomationInputError("operator_input must be empty for this skill")
        return {}
    if spec.scenario == "prepare_content_series":
        if set(raw) != {"brief", "post_count"}:
            raise AutomationInputError(
                "operator_input must contain only brief and post_count"
            )
        try:
            return _normalize_content_series_input(
                raw["brief"],
                raw["post_count"],
            )
        except (TypeError, ValueError) as exc:
            raise AutomationInputError(str(exc)) from exc
    raise AutomationInputError("unsupported automation skill scenario")


def normalize_cadence(
    *,
    kind: str,
    local_time_value: str,
    weekday: int | None,
) -> tuple[str, str, int | None]:
    cadence_kind = str(kind or "").strip()
    local_time_text = str(local_time_value or "").strip()
    if cadence_kind not in {CADENCE_DAILY, CADENCE_WEEKLY}:
        raise AutomationInputError("cadence kind must be daily or weekly")
    if _LOCAL_TIME_RE.fullmatch(local_time_text) is None:
        raise AutomationInputError("local_time must be strict HH:MM")
    if cadence_kind == CADENCE_DAILY:
        if weekday is not None:
            raise AutomationInputError("daily cadence must not include weekday")
        return cadence_kind, local_time_text, None
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
        raise AutomationInputError("weekly cadence requires weekday 0..6")
    return cadence_kind, local_time_text, int(weekday)


def next_occurrence_utc(
    *,
    cadence_kind: str,
    local_time_value: str,
    weekday: int | None,
    timezone_name: str,
    after_utc: datetime,
) -> datetime:
    kind, local_text, normalized_weekday = normalize_cadence(
        kind=cadence_kind,
        local_time_value=local_time_value,
        weekday=weekday,
    )
    after = as_utc(after_utc)
    local_after = to_user_tz(after, timezone_name)
    hour, minute = (int(part) for part in local_text.split(":", 1))

    if kind == CADENCE_DAILY:
        candidate_date = local_after.date()
        candidate = localize_dt(
            datetime.combine(candidate_date, time(hour=hour, minute=minute)),
            timezone_name,
        ).astimezone(timezone.utc)
        if candidate <= after:
            candidate_date = candidate_date + timedelta(days=1)
            candidate = localize_dt(
                datetime.combine(candidate_date, time(hour=hour, minute=minute)),
                timezone_name,
            ).astimezone(timezone.utc)
        return candidate

    assert normalized_weekday is not None
    delta_days = (normalized_weekday - local_after.weekday()) % 7
    candidate_date = local_after.date() + timedelta(days=delta_days)
    candidate = localize_dt(
        datetime.combine(candidate_date, time(hour=hour, minute=minute)),
        timezone_name,
    ).astimezone(timezone.utc)
    if candidate <= after:
        candidate_date = candidate_date + timedelta(days=7)
        candidate = localize_dt(
            datetime.combine(candidate_date, time(hour=hour, minute=minute)),
            timezone_name,
        ).astimezone(timezone.utc)
    return candidate


def automation_definition_fingerprint(
    *,
    skill_id: str,
    skill_version: str,
    operator_input: Mapping[str, object],
    cadence_kind: str,
    local_time_value: str,
    weekday: int | None,
    timezone_name: str,
) -> str:
    payload = {
        "skill_id": str(skill_id),
        "skill_version": str(skill_version),
        "operator_input": dict(operator_input),
        "cadence": {
            "kind": str(cadence_kind),
            "local_time": str(local_time_value),
            "weekday": weekday,
        },
        "timezone": str(timezone_name),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def occurrence_request_id(automation_id: int, scheduled_for: datetime) -> str:
    stamp = as_utc(scheduled_for).strftime("%Y%m%dT%H%M%SZ")
    return f"auto:{int(automation_id)}:{stamp}"


class AdminAgentAutomationService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(
        self,
        *,
        automation_id: int,
        owner_tg_user_id: int,
        channel_id: int,
    ) -> AdminAgentAutomation | None:
        return await self.session.scalar(
            select(AdminAgentAutomation).where(
                AdminAgentAutomation.id == int(automation_id),
                AdminAgentAutomation.owner_tg_user_id == int(owner_tg_user_id),
                AdminAgentAutomation.channel_id == int(channel_id),
            )
        )

    async def list(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        limit: int = 100,
    ) -> list[AdminAgentAutomation]:
        return list(
            (
                await self.session.execute(
                    select(AdminAgentAutomation)
                    .where(
                        AdminAgentAutomation.owner_tg_user_id
                        == int(owner_tg_user_id),
                        AdminAgentAutomation.channel_id == int(channel_id),
                    )
                    .order_by(AdminAgentAutomation.id.desc())
                    .limit(max(1, min(int(limit), 100)))
                )
            ).scalars()
        )

    async def _find_request(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        request_id: str,
    ) -> AdminAgentAutomation | None:
        return await self.session.scalar(
            select(AdminAgentAutomation).where(
                AdminAgentAutomation.owner_tg_user_id == int(owner_tg_user_id),
                AdminAgentAutomation.channel_id == int(channel_id),
                AdminAgentAutomation.request_id == str(request_id),
            )
        )

    async def _ownership_is_current(
        self,
        row: AdminAgentAutomation,
    ) -> bool:
        client = await self.session.scalar(
            select(Client).where(Client.tg_user_id == int(row.owner_tg_user_id))
        )
        if client is None:
            return False
        channel = await self.session.get(Channel, int(row.channel_id))
        return channel is not None and int(channel.owner_id) == int(client.id)

    def _validated_definition(
        self,
        row: AdminAgentAutomation,
    ) -> tuple[AdminAgentSkillSpec | None, str | None]:
        stored_input = (
            dict(row.operator_input)
            if isinstance(row.operator_input, Mapping)
            else {}
        )
        expected = automation_definition_fingerprint(
            skill_id=str(row.skill_id),
            skill_version=str(row.skill_version),
            operator_input=stored_input,
            cadence_kind=str(row.cadence_kind),
            local_time_value=str(row.local_time),
            weekday=row.weekday,
            timezone_name=str(row.timezone),
        )
        if expected != str(row.definition_fingerprint):
            return None, DISABLED_INVALID_DEFINITION
        try:
            spec = SKILL_REGISTRY.resolve(row.skill_id, row.skill_version)
        except KeyError:
            return None, DISABLED_UNSUPPORTED_SKILL_VERSION
        try:
            _ensure_automation_skill(spec)
            normalized = normalize_automation_operator_input(spec, stored_input)
        except AutomationInputError:
            return None, DISABLED_INVALID_DEFINITION
        if normalized != stored_input:
            return None, DISABLED_INVALID_DEFINITION
        return spec, None

    def _migration_suggestion(
        self,
        row: AdminAgentAutomation,
    ) -> dict[str, object] | None:
        try:
            current = SKILL_REGISTRY.current_for_skill_id(str(row.skill_id))
            _ensure_automation_skill(current)
            normalized = normalize_automation_operator_input(
                current,
                row.operator_input if isinstance(row.operator_input, Mapping) else None,
            )
        except (KeyError, AutomationInputError):
            return None
        if str(current.version) == str(row.skill_version):
            return None
        return {
            "migration_available": True,
            "suggested_skill_id": str(current.skill_id),
            "suggested_skill_version": str(current.version),
            "operator_input": normalized,
        }

    async def runs(
        self,
        *,
        automation_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        limit: int = 20,
    ) -> list[AdminAgentRun]:
        return list(
            (
                await self.session.execute(
                    select(AdminAgentRun)
                    .where(
                        AdminAgentRun.automation_id == int(automation_id),
                        AdminAgentRun.owner_tg_user_id == int(owner_tg_user_id),
                        AdminAgentRun.channel_id == int(channel_id),
                    )
                    .order_by(
                        AdminAgentRun.scheduled_for.desc(),
                        AdminAgentRun.id.desc(),
                    )
                    .limit(max(1, min(int(limit), 50)))
                )
            ).scalars()
        )

    async def operational_snapshot(
        self,
        row: AdminAgentAutomation,
        *,
        now_utc: datetime | None = None,
    ) -> dict[str, object]:
        now = as_utc(now_utc)
        owner_current = await self._ownership_is_current(row)
        spec, definition_blocker = self._validated_definition(row)
        durable_reason = str(row.disabled_reason) if row.disabled_reason else None

        latest_rows = await self.runs(
            automation_id=int(row.id),
            owner_tg_user_id=int(row.owner_tg_user_id),
            channel_id=int(row.channel_id),
            limit=1,
        )
        latest = latest_rows[0] if latest_rows else None

        if not owner_current:
            health = HEALTH_BLOCKED
            health_reason = DISABLED_OWNERSHIP_LOST
        elif definition_blocker is not None:
            health = HEALTH_BLOCKED
            health_reason = definition_blocker
        elif durable_reason == DISABLED_MANUAL_PAUSE:
            health = HEALTH_PAUSED
            health_reason = DISABLED_MANUAL_PAUSE
        elif durable_reason in SAFETY_DISABLED_REASONS:
            health = HEALTH_BLOCKED
            health_reason = durable_reason
        elif not bool(row.enabled):
            health = HEALTH_BLOCKED
            health_reason = "disabled_without_reason"
        else:
            health = HEALTH_ACTIVE
            health_reason = "healthy"
            if latest is not None:
                resumable, resume_state = assistant_run_resume_state(
                    latest,
                    now_utc=now,
                )
                phase = str(latest.workflow_phase or "")
                if resumable:
                    health = HEALTH_NEEDS_ATTENTION
                    health_reason = "manual_resume_available"
                elif resume_state == "restart_required" or phase == "restart_required":
                    health = HEALTH_NEEDS_ATTENTION
                    health_reason = "restart_required"
                elif str(latest.status) == "failed" or phase in {"failed", "failed_closed"}:
                    health = HEALTH_NEEDS_ATTENTION
                    health_reason = (
                        "failed_closed" if phase == "failed_closed" else "run_failed"
                    )

        recent = list(
            (
                await self.session.execute(
                    select(AdminAgentRun)
                    .where(
                        AdminAgentRun.automation_id == int(row.id),
                        AdminAgentRun.owner_tg_user_id == int(row.owner_tg_user_id),
                        AdminAgentRun.channel_id == int(row.channel_id),
                        AdminAgentRun.scheduled_for >= now - timedelta(days=30),
                    )
                    .order_by(AdminAgentRun.scheduled_for.desc())
                )
            ).scalars()
        )

        def usage(days: int) -> dict[str, int]:
            cutoff = now - timedelta(days=days)
            rows = [
                run
                for run in recent
                if run.scheduled_for is not None and as_utc(run.scheduled_for) >= cutoff
            ]
            needs_manual = 0
            for run in rows:
                resumable, resume_state = assistant_run_resume_state(run, now_utc=now)
                if resumable or resume_state == "restart_required":
                    needs_manual += 1
            return {
                "occurrence_runs": len(rows),
                "completed": sum(str(run.status) == "completed" for run in rows),
                "failed": sum(
                    str(run.status) == "failed"
                    or str(run.workflow_phase or "") in {"failed", "failed_closed"}
                    for run in rows
                ),
                "restart_required_or_manual_resume": needs_manual,
                "tokens_used": sum(int(run.tokens_used or 0) for run in rows),
            }

        migration = None
        if (
            definition_blocker == DISABLED_UNSUPPORTED_SKILL_VERSION
            or durable_reason == DISABLED_UNSUPPORTED_SKILL_VERSION
        ):
            migration = self._migration_suggestion(row)

        execution_limits = (
            {str(key): value for key, value in spec.execution_limits.items()}
            if spec is not None
            else None
        )
        post_count = None
        if (
            spec is not None
            and spec.scenario == "prepare_content_series"
            and isinstance(row.operator_input, Mapping)
            and isinstance(row.operator_input.get("post_count"), int)
        ):
            post_count = int(row.operator_input["post_count"])

        claimed_at = row.claimed_at
        claim_active = bool(
            row.claim_token
            and claimed_at is not None
            and as_utc(claimed_at) > now - CLAIM_LEASE
        )
        return {
            "health": health,
            "health_reason": health_reason,
            "claim_active": claim_active,
            "claimed_at": claimed_at,
            "latest_run": latest,
            "usage_7d": usage(7),
            "usage_30d": usage(30),
            "execution_limits": execution_limits,
            "cadence_occurrences_per_week": 7 if str(row.cadence_kind) == CADENCE_DAILY else 1,
            "post_count": post_count,
            "migration_suggestion": migration,
        }

    async def create(
        self,
        *,
        owner_tg_user_id: int,
        channel_id: int,
        request_id: str,
        skill_id: str,
        skill_version: str,
        operator_input: Mapping[str, object] | None,
        cadence_kind: str,
        local_time_value: str,
        weekday: int | None,
        now_utc: datetime | None = None,
    ) -> AdminAgentAutomation:
        request_key = str(request_id or "").strip()
        if _REQUEST_ID_RE.fullmatch(request_key) is None:
            raise AutomationInputError("request_id is invalid")

        try:
            spec = SKILL_REGISTRY.resolve(skill_id, skill_version)
        except KeyError as exc:
            raise AutomationInputError("unknown skill version") from exc
        _ensure_automation_skill(spec)
        normalized_input = normalize_automation_operator_input(spec, operator_input)
        kind, local_text, normalized_weekday = normalize_cadence(
            kind=cadence_kind,
            local_time_value=local_time_value,
            weekday=weekday,
        )

        existing = await self._find_request(
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
            request_id=request_key,
        )
        if existing is not None:
            expected = automation_definition_fingerprint(
                skill_id=spec.skill_id,
                skill_version=spec.version,
                operator_input=normalized_input,
                cadence_kind=kind,
                local_time_value=local_text,
                weekday=normalized_weekday,
                timezone_name=str(existing.timezone),
            )
            if expected != str(existing.definition_fingerprint):
                raise AutomationIdempotencyConflict(
                    "request_id already exists with different automation definition"
                )
            return existing

        timezone_name = await _resolve_channel_timezone(self.session, int(channel_id))
        definition_fingerprint = automation_definition_fingerprint(
            skill_id=spec.skill_id,
            skill_version=spec.version,
            operator_input=normalized_input,
            cadence_kind=kind,
            local_time_value=local_text,
            weekday=normalized_weekday,
            timezone_name=timezone_name,
        )
        now = as_utc(now_utc)
        next_run_at = next_occurrence_utc(
            cadence_kind=kind,
            local_time_value=local_text,
            weekday=normalized_weekday,
            timezone_name=timezone_name,
            after_utc=now,
        )
        row = AdminAgentAutomation(
            owner_tg_user_id=int(owner_tg_user_id),
            channel_id=int(channel_id),
            skill_id=spec.skill_id,
            skill_version=str(spec.version),
            operator_input=normalized_input,
            cadence_kind=kind,
            local_time=local_text,
            weekday=normalized_weekday,
            timezone=timezone_name,
            enabled=True,
            next_run_at=next_run_at,
            last_scheduled_for=None,
            claim_token=None,
            claimed_at=None,
            request_id=request_key,
            definition_fingerprint=definition_fingerprint,
        )
        self.session.add(row)
        try:
            await self.session.commit()
            await self.session.refresh(row)
            return row
        except IntegrityError:
            await self.session.rollback()
            existing = await self._find_request(
                owner_tg_user_id=owner_tg_user_id,
                channel_id=channel_id,
                request_id=request_key,
            )
            if existing is None:
                raise
            expected = automation_definition_fingerprint(
                skill_id=spec.skill_id,
                skill_version=spec.version,
                operator_input=normalized_input,
                cadence_kind=kind,
                local_time_value=local_text,
                weekday=normalized_weekday,
                timezone_name=str(existing.timezone),
            )
            if expected != str(existing.definition_fingerprint):
                raise AutomationIdempotencyConflict(
                    "request_id already exists with different automation definition"
                )
            return existing

    async def set_enabled(
        self,
        *,
        automation_id: int,
        owner_tg_user_id: int,
        channel_id: int,
        enabled: bool,
        now_utc: datetime | None = None,
    ) -> AdminAgentAutomation | None:
        row = await self.get(
            automation_id=automation_id,
            owner_tg_user_id=owner_tg_user_id,
            channel_id=channel_id,
        )
        if row is None:
            return None

        now = as_utc(now_utc)
        value = bool(enabled)
        if not value:
            row.enabled = False
            row.disabled_reason = DISABLED_MANUAL_PAUSE
            row.disabled_at = now
            row.claim_token = None
            row.claimed_at = None
            await self.session.commit()
            await self.session.refresh(row)
            return row

        if not await self._ownership_is_current(row):
            row.enabled = False
            row.disabled_reason = DISABLED_OWNERSHIP_LOST
            row.disabled_at = now
            row.last_outcome = OUTCOME_SAFETY_DISABLED
            row.last_outcome_at = now
            row.claim_token = None
            row.claimed_at = None
            await self.session.commit()
            raise AutomationControlConflict(DISABLED_OWNERSHIP_LOST)

        _spec, blocker = self._validated_definition(row)
        if blocker is not None:
            row.enabled = False
            row.disabled_reason = blocker
            row.disabled_at = now
            row.last_outcome = OUTCOME_SAFETY_DISABLED
            row.last_outcome_at = now
            row.claim_token = None
            row.claimed_at = None
            await self.session.commit()
            raise AutomationControlConflict(blocker)

        row.enabled = True
        row.disabled_reason = None
        row.disabled_at = None
        row.next_run_at = next_occurrence_utc(
            cadence_kind=str(row.cadence_kind),
            local_time_value=str(row.local_time),
            weekday=row.weekday,
            timezone_name=str(row.timezone),
            after_utc=now,
        )
        row.claim_token = None
        row.claimed_at = None
        await self.session.commit()
        await self.session.refresh(row)
        return row


class AdminAgentAutomationTickService:
    """Deterministic E4 due-check invoked only by the existing AI-auto heartbeat."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        *,
        max_occurrences: int = MAX_OCCURRENCES_PER_TICK,
    ):
        self.session_factory = session_factory
        self.max_occurrences = max(1, min(int(max_occurrences), MAX_OCCURRENCES_PER_TICK))

    async def _claim_one(
        self,
        now_utc: datetime,
    ) -> tuple[int, str] | None:
        now = as_utc(now_utc)
        lease_cutoff = now - CLAIM_LEASE
        async with self.session_factory() as session:
            candidates = list(
                (
                    await session.execute(
                        select(AdminAgentAutomation)
                        .where(
                            AdminAgentAutomation.enabled.is_(True),
                            AdminAgentAutomation.next_run_at <= now,
                            or_(
                                AdminAgentAutomation.claim_token.is_(None),
                                AdminAgentAutomation.claimed_at.is_(None),
                                AdminAgentAutomation.claimed_at <= lease_cutoff,
                            ),
                        )
                        .order_by(
                            AdminAgentAutomation.next_run_at.asc(),
                            AdminAgentAutomation.id.asc(),
                        )
                        .limit(self.max_occurrences)
                    )
                ).scalars()
            )
            for row in candidates:
                old_token = row.claim_token
                token_clause = (
                    AdminAgentAutomation.claim_token == str(old_token)
                    if old_token is not None
                    else AdminAgentAutomation.claim_token.is_(None)
                )
                token = uuid4().hex
                transition = await session.execute(
                    update(AdminAgentAutomation)
                    .where(
                        AdminAgentAutomation.id == int(row.id),
                        AdminAgentAutomation.enabled.is_(True),
                        AdminAgentAutomation.next_run_at == row.next_run_at,
                        token_clause,
                    )
                    .values(claim_token=token, claimed_at=now)
                )
                await session.commit()
                if int(transition.rowcount or 0) == 1:
                    return int(row.id), token
        return None

    async def _ownership_is_current(
        self,
        session: AsyncSession,
        row: AdminAgentAutomation,
    ) -> bool:
        client = await session.scalar(
            select(Client).where(
                Client.tg_user_id == int(row.owner_tg_user_id)
            )
        )
        if client is None:
            return False
        channel = await session.get(Channel, int(row.channel_id))
        return channel is not None and int(channel.owner_id) == int(client.id)

    async def _disable_claimed(
        self,
        session: AsyncSession,
        row: AdminAgentAutomation,
        *,
        claim_token: str,
        reason: str,
        now_utc: datetime,
    ) -> None:
        now = as_utc(now_utc)
        await session.execute(
            update(AdminAgentAutomation)
            .where(
                AdminAgentAutomation.id == int(row.id),
                AdminAgentAutomation.claim_token == str(claim_token),
            )
            .values(
                enabled=False,
                disabled_reason=str(reason),
                disabled_at=now,
                last_outcome=OUTCOME_SAFETY_DISABLED,
                last_outcome_at=now,
                claim_token=None,
                claimed_at=None,
            )
        )
        await session.commit()

    async def _advance_claimed(
        self,
        session: AsyncSession,
        row: AdminAgentAutomation,
        *,
        claim_token: str,
        scheduled_for: datetime,
        after_utc: datetime,
        outcome: str = OUTCOME_RUN_RECORDED,
    ) -> None:
        next_run_at = next_occurrence_utc(
            cadence_kind=str(row.cadence_kind),
            local_time_value=str(row.local_time),
            weekday=row.weekday,
            timezone_name=str(row.timezone),
            after_utc=as_utc(after_utc),
        )
        await session.execute(
            update(AdminAgentAutomation)
            .where(
                AdminAgentAutomation.id == int(row.id),
                AdminAgentAutomation.claim_token == str(claim_token),
                AdminAgentAutomation.next_run_at == row.next_run_at,
            )
            .values(
                last_scheduled_for=as_utc(scheduled_for),
                next_run_at=next_run_at,
                last_outcome=str(outcome),
                last_outcome_at=as_utc(after_utc),
                claim_token=None,
                claimed_at=None,
            )
        )
        await session.commit()

    async def _process_claim(
        self,
        *,
        automation_id: int,
        claim_token: str,
        now_utc: datetime,
    ) -> None:
        now = as_utc(now_utc)
        async with self.session_factory() as session:
            row = await session.get(AdminAgentAutomation, int(automation_id))
            if (
                row is None
                or not bool(row.enabled)
                or str(row.claim_token or "") != str(claim_token)
            ):
                return

            scheduled_for = as_utc(row.next_run_at)
            if now - scheduled_for > MISFIRE_GRACE:
                await self._advance_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                    scheduled_for=scheduled_for,
                    after_utc=now,
                    outcome=OUTCOME_MISFIRE_SKIPPED,
                )
                return

            stored_input = (
                dict(row.operator_input)
                if isinstance(row.operator_input, Mapping)
                else {}
            )
            expected_fingerprint = automation_definition_fingerprint(
                skill_id=str(row.skill_id),
                skill_version=str(row.skill_version),
                operator_input=stored_input,
                cadence_kind=str(row.cadence_kind),
                local_time_value=str(row.local_time),
                weekday=row.weekday,
                timezone_name=str(row.timezone),
            )
            if expected_fingerprint != str(row.definition_fingerprint):
                await self._disable_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                    reason=DISABLED_INVALID_DEFINITION,
                    now_utc=now,
                )
                return

            try:
                spec = SKILL_REGISTRY.resolve(row.skill_id, row.skill_version)
            except KeyError:
                await self._disable_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                    reason=DISABLED_UNSUPPORTED_SKILL_VERSION,
                    now_utc=now,
                )
                return
            try:
                _ensure_automation_skill(spec)
                normalized_input = normalize_automation_operator_input(spec, stored_input)
            except AutomationInputError:
                await self._disable_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                    reason=DISABLED_INVALID_DEFINITION,
                    now_utc=now,
                )
                return
            if normalized_input != stored_input:
                await self._disable_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                    reason=DISABLED_INVALID_DEFINITION,
                    now_utc=now,
                )
                return

            if not await self._ownership_is_current(session, row):
                await self._disable_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                    reason=DISABLED_OWNERSHIP_LOST,
                    now_utc=now,
                )
                return

            existing_run = await session.scalar(
                select(AdminAgentRun).where(
                    AdminAgentRun.automation_id == int(row.id),
                    AdminAgentRun.scheduled_for == scheduled_for,
                )
            )
            if existing_run is not None:
                await self._advance_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                    scheduled_for=scheduled_for,
                    after_utc=scheduled_for,
                )
                return

            request_id = occurrence_request_id(int(row.id), scheduled_for)
            runner = AdminAgentRunner(
                session,
                now_utc=scheduled_for,
            )
            await runner.run_exact_skill(
                skill=spec,
                channel_id=int(row.channel_id),
                owner_tg_user_id=int(row.owner_tg_user_id),
                request_id=request_id,
                operator_input=normalized_input,
                automation_id=int(row.id),
                scheduled_for=scheduled_for,
            )
            await session.refresh(row)
            if str(row.claim_token or "") != str(claim_token):
                return
            await self._advance_claimed(
                session,
                row,
                claim_token=claim_token,
                scheduled_for=scheduled_for,
                after_utc=scheduled_for,
            )

    async def tick(self, *, now_utc: datetime | None = None) -> int:
        now = as_utc(now_utc)
        processed = 0
        for _ in range(self.max_occurrences):
            claim = await self._claim_one(now)
            if claim is None:
                break
            automation_id, token = claim
            try:
                await self._process_claim(
                    automation_id=automation_id,
                    claim_token=token,
                    now_utc=now,
                )
            except Exception:
                # Leave the durable lease intact. A later heartbeat may recover it
                # after expiry; if a run already exists, no-replay reconciliation
                # advances the occurrence without a second provider call.
                logger.exception(
                    "AdminAgentAutomationTickService: occurrence failed for automation {}",
                    automation_id,
                )
            processed += 1
        return processed
