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
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_LOCAL_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AutomationInputError(ValueError):
    pass


class AutomationIdempotencyConflict(RuntimeError):
    pass


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
        value = bool(enabled)
        if value and not bool(row.enabled):
            row.next_run_at = next_occurrence_utc(
                cadence_kind=str(row.cadence_kind),
                local_time_value=str(row.local_time),
                weekday=row.weekday,
                timezone_name=str(row.timezone),
                after_utc=as_utc(now_utc),
            )
        row.enabled = value
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
    ) -> None:
        await session.execute(
            update(AdminAgentAutomation)
            .where(
                AdminAgentAutomation.id == int(row.id),
                AdminAgentAutomation.claim_token == str(claim_token),
            )
            .values(
                enabled=False,
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
                )
                return

            try:
                spec = SKILL_REGISTRY.resolve(row.skill_id, row.skill_version)
                _ensure_automation_skill(spec)
                normalized_input = normalize_automation_operator_input(
                    spec,
                    row.operator_input if isinstance(row.operator_input, Mapping) else None,
                )
            except (KeyError, AutomationInputError):
                await self._disable_claimed(
                    session,
                    row,
                    claim_token=claim_token,
                )
                return

            if not await self._ownership_is_current(session, row):
                await self._disable_claimed(
                    session,
                    row,
                    claim_token=claim_token,
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
