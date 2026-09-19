from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.studio.auth import StudioPrincipal, require_studio_principal
from app.core.db import AsyncSessionLocal
from app.domain.admin_agent import (
    AdminAgentApproval,
    AdminAgentApprovalBatch,
    AdminAgentAutomation,
    AdminAgentEvent,
    AdminAgentRun,
    AdminAgentRunArtifact,
)
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo
from app.services.admin_agent import (
    DRAFT_SCENARIO_LIMITS,
    SERIES_SCENARIO_LIMITS,
    AgentExecutionBusy,
    AgentIdempotencyConflict,
    AgentResumeError,
    AdminAgentRunner,
    PHASE_DRAFTS_PERSISTED,
    PHASE_SERIES_PERSISTED,
    SCENARIO_ATTENTION_TODAY,
    SCENARIO_DRAFTS_TOMORROW,
    SCENARIO_PREPARE_CONTENT_SERIES,
    assistant_run_resume_state,
)
from app.services.admin_agent_automations import (
    AutomationControlConflict,
    AutomationIdempotencyConflict,
    AutomationInputError,
    AdminAgentAutomationService,
)
from app.services.admin_agent_approvals import (
    ACTION_SCHEDULE_DRAFT_TOMORROW,
    ApprovalExecutionError,
    ApprovalInputError,
    ApprovalStateConflict,
    AdminAgentApprovalService,
)
from app.services.admin_agent_series_approvals import (
    ACTION_SCHEDULE_CONTENT_SERIES,
    AdminAgentSeriesApprovalService,
    SeriesApprovalExecutionError,
    SeriesApprovalIdempotencyConflict,
    SeriesApprovalInputError,
    SeriesApprovalStateConflict,
)
from app.services.admin_agent_skills import (
    AUTOMATION_BOUNDED,
    RESUME_NONE,
    AdminAgentSkillSpec,
    SKILL_REGISTRY,
)


router = APIRouter(prefix="/api/studio", tags=["assistant"])


async def _session_dependency() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session_dependency)]
PrincipalDep = Annotated[StudioPrincipal, Depends(require_studio_principal)]


class ContentSeriesOperatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(min_length=20, max_length=2000)
    post_count: int = Field(ge=2, le=8, strict=True)

    @field_validator("brief", mode="before")
    @classmethod
    def normalize_brief(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class AssistantRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal[
        "attention_today",
        "drafts_tomorrow",
        "prepare_content_series",
    ] = SCENARIO_ATTENTION_TODAY
    request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    operator_input: ContentSeriesOperatorInput | None = None

    @model_validator(mode="after")
    def validate_scenario_input(self) -> "AssistantRunRequest":
        if self.scenario == SCENARIO_PREPARE_CONTENT_SERIES:
            if not self.request_id:
                raise ValueError("request_id is required for prepare_content_series")
            if self.operator_input is None:
                raise ValueError("operator_input is required for prepare_content_series")
        elif self.operator_input is not None:
            raise ValueError("operator_input is not supported for this scenario")
        return self


class AssistantSkillResponse(BaseModel):
    skill_id: str
    version: str
    scenario: Literal[
        "attention_today",
        "drafts_tomorrow",
        "prepare_content_series",
    ]
    display_title: str
    description: str
    category: str
    operator_input_schema: dict[str, Any]
    result_kind: str
    capability_classes: list[str]
    capability_summary: str
    context_profile: str
    context_requirements: str
    resume_policy: str
    resumable: bool
    approval_requirement: str
    automation_policy: str
    automation_allowed: bool
    execution_limits: dict[str, int | float]


class EmptyAutomationOperatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantAutomationCadenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["daily", "weekly"]
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, ge=0, le=6, strict=True)

    @model_validator(mode="after")
    def validate_weekday(self) -> "AssistantAutomationCadenceRequest":
        if self.kind == "daily" and self.weekday is not None:
            raise ValueError("daily cadence must not include weekday")
        if self.kind == "weekly" and self.weekday is None:
            raise ValueError("weekly cadence requires weekday")
        return self


class AssistantAutomationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    skill_id: str = Field(min_length=1, max_length=64)
    skill_version: str = Field(min_length=1, max_length=32)
    operator_input: EmptyAutomationOperatorInput | ContentSeriesOperatorInput
    cadence: AssistantAutomationCadenceRequest


class AssistantAutomationEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AssistantAutomationCadenceResponse(BaseModel):
    kind: Literal["daily", "weekly"]
    local_time: str
    weekday: int | None = None


class AssistantAutomationRunSummaryResponse(BaseModel):
    id: int
    scheduled_for: datetime | None
    status: str
    workflow_phase: str | None
    resumable: bool
    resume_state: str
    tokens_used: int
    model: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    result_kind: str | None
    result_metadata: dict[str, Any]


class AssistantAutomationUsageResponse(BaseModel):
    occurrence_runs: int
    completed: int
    failed: int
    restart_required_or_manual_resume: int
    tokens_used: int


class AssistantAutomationResponse(BaseModel):
    id: int
    channel_id: int
    skill_id: str
    skill_version: str
    operator_input: dict[str, Any]
    automation_policy: str
    cadence: AssistantAutomationCadenceResponse
    timezone: str
    enabled: bool
    disabled_reason: str | None
    disabled_at: datetime | None
    next_run_at: datetime
    last_scheduled_for: datetime | None
    last_outcome: str | None
    last_outcome_at: datetime | None
    health: Literal["active", "paused", "blocked", "needs_attention"]
    health_reason: str
    claim_active: bool
    claimed_at: datetime | None
    latest_run: AssistantAutomationRunSummaryResponse | None
    usage_7d: AssistantAutomationUsageResponse
    usage_30d: AssistantAutomationUsageResponse
    execution_limits: dict[str, int | float] | None
    cadence_occurrences_per_week: int
    post_count: int | None
    migration_available: bool
    suggested_skill_id: str | None
    suggested_skill_version: str | None
    request_id: str
    definition_fingerprint: str
    created_at: datetime | None
    updated_at: datetime | None


class AssistantResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantApprovalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_item_id: int = Field(gt=0)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class AssistantApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantSeriesApprovalSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(ge=1, le=8, strict=True)
    local_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AssistantSeriesApprovalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_id: int = Field(gt=0, strict=True)
    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    slots: list[AssistantSeriesApprovalSlot] = Field(min_length=2, max_length=8)


class AssistantSeriesApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantSeriesApprovalItemResponse(BaseModel):
    id: int
    ordinal: int
    content_item_id: int
    captured_content_revision: int
    content_title: str
    local_date: str
    local_time: str
    resolved_scheduled_at: datetime
    item_fingerprint: str
    execution_key: str
    state: str
    schedule_entry_id: int | None
    publication_id: int | None
    failure_reason: str | None
    execution_started_at: datetime | None
    executed_at: datetime | None


class AssistantSeriesApprovalResponse(BaseModel):
    id: int
    channel_id: int
    source_run_id: int
    action_type: str
    state: str
    request_id: str
    timezone: str
    item_count: int
    series_title: str
    source_plan_fingerprint: str
    action_fingerprint: str
    execution_key: str
    reviewer_tg_user_id: int | None
    failure_reason: str | None
    reviewed_at: datetime | None
    executed_at: datetime | None
    created_at: datetime | None
    items: list[AssistantSeriesApprovalItemResponse]


class AssistantApprovalResponse(BaseModel):
    id: int
    channel_id: int
    source_admin_agent_run_id: int | None
    action_type: str
    state: str
    content_item_id: int
    content_revision: int
    timezone: str
    target_local_date: str
    local_time: str
    resolved_scheduled_at: datetime
    action_fingerprint: str
    execution_key: str | None
    request_id: str
    schedule_entry_id: int | None
    publication_id: int | None
    reviewer_tg_user_id: int | None
    failure_reason: str | None
    reviewed_at: datetime | None
    executed_at: datetime | None
    created_at: datetime | None


class AssistantEventResponse(BaseModel):
    id: int
    sequence: int
    event_type: str
    tool_name: str | None
    payload: dict[str, Any]
    created_at: datetime | None


class AssistantRunResponse(BaseModel):
    id: int
    channel_id: int
    scenario: str
    request_id: str | None
    operator_input: dict[str, Any] | None
    skill_id: str | None
    skill_version: str | None
    workflow_phase: str | None
    resumable: bool
    resume_state: str
    status: str
    model: str | None
    tokens_used: int
    result: dict[str, Any] | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime | None
    events: list[AssistantEventResponse] = Field(default_factory=list)


async def _require_owned_channel(
    session: AsyncSession,
    principal: StudioPrincipal,
    channel_id: int,
) -> None:
    client = await ClientsRepo(session).create_or_get(
        principal.tg_user_id,
        principal.username,
        principal.full_name,
    )
    channel = await ChannelsRepo(session).get_by_id(int(channel_id))
    if channel is None or int(channel.owner_id) != int(client.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _skill_response(spec: AdminAgentSkillSpec) -> AssistantSkillResponse:
    return AssistantSkillResponse(
        skill_id=spec.skill_id,
        version=str(spec.version),
        scenario=spec.scenario,
        display_title=spec.display_title,
        description=spec.description,
        category=spec.category,
        operator_input_schema=_plain_json(spec.operator_input_schema),
        result_kind=spec.result_kind,
        capability_classes=list(spec.allowed_capability_classes),
        capability_summary=spec.capability_summary,
        context_profile=spec.context_profile,
        context_requirements=spec.context_requirements,
        resume_policy=spec.resume_policy,
        resumable=spec.resume_policy != RESUME_NONE,
        approval_requirement=spec.approval_requirement,
        automation_policy=spec.automation_policy,
        automation_allowed=spec.automation_policy == AUTOMATION_BOUNDED,
        execution_limits={
            str(key): value for key, value in spec.execution_limits.items()
        },
    )


def _automation_run_summary(
    row: AdminAgentRun,
) -> AssistantAutomationRunSummaryResponse:
    resumable, resume_state = assistant_run_resume_state(row)
    result = row.result if isinstance(row.result, dict) else {}
    result_metadata: dict[str, Any] = {}
    for key in (
        "draft_count",
        "requested_post_count",
        "generated_by",
        "write_capability",
    ):
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if key in result:
                result_metadata[key] = value
    result_kind = None
    try:
        result_kind = SKILL_REGISTRY.resolve(row.skill_id, row.skill_version).result_kind
    except KeyError:
        scenario = result.get("scenario")
        if isinstance(scenario, str):
            result_kind = scenario
    return AssistantAutomationRunSummaryResponse(
        id=int(row.id),
        scheduled_for=row.scheduled_for,
        status=str(row.status),
        workflow_phase=row.workflow_phase,
        resumable=resumable,
        resume_state=resume_state,
        tokens_used=int(row.tokens_used or 0),
        model=row.model,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        result_kind=result_kind,
        result_metadata=result_metadata,
    )


async def _automation_response(
    session: AsyncSession,
    row: AdminAgentAutomation,
) -> AssistantAutomationResponse:
    snapshot = await AdminAgentAutomationService(session).operational_snapshot(row)
    latest = snapshot["latest_run"]
    migration = snapshot["migration_suggestion"]
    return AssistantAutomationResponse(
        id=int(row.id),
        channel_id=int(row.channel_id),
        skill_id=str(row.skill_id),
        skill_version=str(row.skill_version),
        operator_input=dict(row.operator_input or {}),
        automation_policy=AUTOMATION_BOUNDED,
        cadence=AssistantAutomationCadenceResponse(
            kind=str(row.cadence_kind),
            local_time=str(row.local_time),
            weekday=(int(row.weekday) if row.weekday is not None else None),
        ),
        timezone=str(row.timezone),
        enabled=bool(row.enabled),
        disabled_reason=row.disabled_reason,
        disabled_at=row.disabled_at,
        next_run_at=row.next_run_at,
        last_scheduled_for=row.last_scheduled_for,
        last_outcome=row.last_outcome,
        last_outcome_at=row.last_outcome_at,
        health=str(snapshot["health"]),
        health_reason=str(snapshot["health_reason"]),
        claim_active=bool(snapshot["claim_active"]),
        claimed_at=snapshot["claimed_at"],
        latest_run=(
            _automation_run_summary(latest)
            if isinstance(latest, AdminAgentRun)
            else None
        ),
        usage_7d=AssistantAutomationUsageResponse(**snapshot["usage_7d"]),
        usage_30d=AssistantAutomationUsageResponse(**snapshot["usage_30d"]),
        execution_limits=snapshot["execution_limits"],
        cadence_occurrences_per_week=int(snapshot["cadence_occurrences_per_week"]),
        post_count=(
            int(snapshot["post_count"])
            if snapshot["post_count"] is not None
            else None
        ),
        migration_available=isinstance(migration, dict),
        suggested_skill_id=(
            str(migration["suggested_skill_id"])
            if isinstance(migration, dict)
            else None
        ),
        suggested_skill_version=(
            str(migration["suggested_skill_version"])
            if isinstance(migration, dict)
            else None
        ),
        request_id=str(row.request_id),
        definition_fingerprint=str(row.definition_fingerprint),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event_response(row: AdminAgentEvent) -> AssistantEventResponse:
    return AssistantEventResponse(
        id=int(row.id),
        sequence=int(row.sequence),
        event_type=str(row.event_type),
        tool_name=row.tool_name,
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )


async def _response(
    session: AsyncSession,
    row: AdminAgentRun,
    *,
    include_events: bool,
) -> AssistantRunResponse:
    events: list[AssistantEventResponse] = []
    if include_events:
        event_rows = list(
            (
                await session.execute(
                    select(AdminAgentEvent)
                    .where(AdminAgentEvent.run_id == int(row.id))
                    .order_by(AdminAgentEvent.sequence.asc())
                    .limit(100)
                )
            ).scalars()
        )
        events = [_event_response(event) for event in event_rows]
    resumable, resume_state = assistant_run_resume_state(row)
    persisted_phase = str(row.workflow_phase or "")
    if resumable and persisted_phase in {PHASE_DRAFTS_PERSISTED, PHASE_SERIES_PERSISTED}:
        artifact_rows = list(
            (
                await session.execute(
                    select(AdminAgentRunArtifact)
                    .where(AdminAgentRunArtifact.run_id == int(row.id))
                    .order_by(AdminAgentRunArtifact.ordinal.asc())
                )
            ).scalars()
        )
        if persisted_phase == PHASE_DRAFTS_PERSISTED:
            expected_count = 3
            expected_type = "content_draft"
        else:
            operator_input = row.operator_input if isinstance(row.operator_input, dict) else {}
            raw_count = operator_input.get("post_count")
            expected_count = int(raw_count) if isinstance(raw_count, int) else 0
            expected_type = "series_draft"
        valid_artifacts = (
            expected_count > 0
            and len(artifact_rows) == expected_count
            and [int(value.ordinal) for value in artifact_rows]
            == list(range(1, expected_count + 1))
            and all(str(value.artifact_type) == expected_type for value in artifact_rows)
        )
        if not valid_artifacts:
            resumable = False
            resume_state = "partial_artifacts"

    return AssistantRunResponse(
        id=int(row.id),
        channel_id=int(row.channel_id),
        scenario=str(row.scenario),
        request_id=row.request_id,
        operator_input=(
            dict(row.operator_input) if isinstance(row.operator_input, dict) else None
        ),
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        workflow_phase=row.workflow_phase,
        resumable=resumable,
        resume_state=resume_state,
        status=str(row.status),
        model=row.model,
        tokens_used=int(row.tokens_used or 0),
        result=dict(row.result) if isinstance(row.result, dict) else None,
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        events=events,
    )


def _approval_response(row: AdminAgentApproval) -> AssistantApprovalResponse:
    return AssistantApprovalResponse(
        id=int(row.id),
        channel_id=int(row.channel_id),
        source_admin_agent_run_id=(
            int(row.source_admin_agent_run_id)
            if row.source_admin_agent_run_id is not None
            else None
        ),
        action_type=str(row.action_type),
        state=str(row.state),
        content_item_id=int(row.content_item_id),
        content_revision=int(row.content_revision),
        timezone=str(row.timezone),
        target_local_date=row.target_local_date.isoformat(),
        local_time=str(row.local_time),
        resolved_scheduled_at=row.resolved_scheduled_at,
        action_fingerprint=str(row.action_fingerprint),
        execution_key=row.execution_key,
        request_id=str(row.request_id),
        schedule_entry_id=(
            int(row.schedule_entry_id) if row.schedule_entry_id is not None else None
        ),
        publication_id=(
            int(row.publication_id) if row.publication_id is not None else None
        ),
        reviewer_tg_user_id=(
            int(row.reviewer_tg_user_id)
            if row.reviewer_tg_user_id is not None
            else None
        ),
        failure_reason=row.failure_reason,
        reviewed_at=row.reviewed_at,
        executed_at=row.executed_at,
        created_at=row.created_at,
    )


async def _series_approval_response(
    session: AsyncSession,
    row: AdminAgentApprovalBatch,
) -> AssistantSeriesApprovalResponse:
    item_rows = await AdminAgentSeriesApprovalService(session).items_for_batch(int(row.id))
    items = [
        AssistantSeriesApprovalItemResponse(
            id=int(item.id),
            ordinal=int(item.ordinal),
            content_item_id=int(item.content_item_id),
            captured_content_revision=int(item.captured_content_revision),
            content_title=str(item.content_title),
            local_date=item.local_date.isoformat(),
            local_time=str(item.local_time),
            resolved_scheduled_at=item.resolved_scheduled_at,
            item_fingerprint=str(item.item_fingerprint),
            execution_key=str(item.execution_key),
            state=str(item.state),
            schedule_entry_id=(
                int(item.schedule_entry_id)
                if item.schedule_entry_id is not None
                else None
            ),
            publication_id=(
                int(item.publication_id) if item.publication_id is not None else None
            ),
            failure_reason=item.failure_reason,
            execution_started_at=item.execution_started_at,
            executed_at=item.executed_at,
        )
        for item in item_rows
    ]
    return AssistantSeriesApprovalResponse(
        id=int(row.id),
        channel_id=int(row.channel_id),
        source_run_id=int(row.source_run_id),
        action_type=str(row.action_type),
        state=str(row.state),
        request_id=str(row.request_id),
        timezone=str(row.timezone),
        item_count=int(row.item_count),
        series_title=str(row.series_title),
        source_plan_fingerprint=str(row.source_plan_fingerprint),
        action_fingerprint=str(row.action_fingerprint),
        execution_key=str(row.execution_key),
        reviewer_tg_user_id=(
            int(row.reviewer_tg_user_id)
            if row.reviewer_tg_user_id is not None
            else None
        ),
        failure_reason=row.failure_reason,
        reviewed_at=row.reviewed_at,
        executed_at=row.executed_at,
        created_at=row.created_at,
        items=items,
    )



@router.get(
    "/channels/{channel_id}/assistant/skills",
    response_model=list[AssistantSkillResponse],
)
async def list_assistant_skills(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> list[AssistantSkillResponse]:
    await _require_owned_channel(session, principal, channel_id)
    return [_skill_response(spec) for spec in SKILL_REGISTRY.current_specs]


@router.get(
    "/channels/{channel_id}/assistant/automations",
    response_model=list[AssistantAutomationResponse],
)
async def list_assistant_automations(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> list[AssistantAutomationResponse]:
    await _require_owned_channel(session, principal, channel_id)
    rows = await AdminAgentAutomationService(session).list(
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
    )
    return [await _automation_response(session, row) for row in rows]


@router.post(
    "/channels/{channel_id}/assistant/automations",
    response_model=AssistantAutomationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assistant_automation(
    channel_id: int,
    request: AssistantAutomationCreateRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantAutomationResponse:
    await _require_owned_channel(session, principal, channel_id)
    operator_input = request.operator_input.model_dump()
    try:
        row = await AdminAgentAutomationService(session).create(
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            request_id=request.request_id,
            skill_id=request.skill_id,
            skill_version=request.skill_version,
            operator_input=operator_input,
            cadence_kind=request.cadence.kind,
            local_time_value=request.cadence.local_time,
            weekday=request.cadence.weekday,
        )
    except AutomationInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AutomationIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _automation_response(session, row)


@router.get(
    "/channels/{channel_id}/assistant/automations/{automation_id}",
    response_model=AssistantAutomationResponse,
)
async def get_assistant_automation(
    channel_id: int,
    automation_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantAutomationResponse:
    await _require_owned_channel(session, principal, channel_id)
    row = await AdminAgentAutomationService(session).get(
        automation_id=automation_id,
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    return await _automation_response(session, row)


@router.post(
    "/channels/{channel_id}/assistant/automations/{automation_id}/enabled",
    response_model=AssistantAutomationResponse,
)
async def set_assistant_automation_enabled(
    channel_id: int,
    automation_id: int,
    request: AssistantAutomationEnabledRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantAutomationResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        row = await AdminAgentAutomationService(session).set_enabled(
            automation_id=automation_id,
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            enabled=request.enabled,
        )
    except AutomationControlConflict as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    return await _automation_response(session, row)


@router.get(
    "/channels/{channel_id}/assistant/automations/{automation_id}/runs",
    response_model=list[AssistantAutomationRunSummaryResponse],
)
async def list_assistant_automation_runs(
    channel_id: int,
    automation_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[AssistantAutomationRunSummaryResponse]:
    await _require_owned_channel(session, principal, channel_id)
    service = AdminAgentAutomationService(session)
    row = await service.get(
        automation_id=automation_id,
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    runs = await service.runs(
        automation_id=automation_id,
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
        limit=limit,
    )
    return [_automation_run_summary(run) for run in runs]


@router.post(
    "/channels/{channel_id}/assistant/approvals",
    response_model=AssistantApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assistant_approval(
    channel_id: int,
    request: AssistantApprovalCreateRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        row = await AdminAgentApprovalService(session).create_schedule_draft_tomorrow(
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            content_item_id=request.content_item_id,
            local_time_value=request.local_time,
            request_id=request.request_id,
        )
    except ApprovalInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if str(row.action_type) != ACTION_SCHEDULE_DRAFT_TOMORROW:
        raise HTTPException(status_code=409, detail="Unsupported approval action")
    return _approval_response(row)


@router.get(
    "/channels/{channel_id}/assistant/approvals",
    response_model=list[AssistantApprovalResponse],
)
async def list_assistant_approvals(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> list[AssistantApprovalResponse]:
    await _require_owned_channel(session, principal, channel_id)
    rows = await AdminAgentApprovalService(session).list(
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
        limit=limit,
    )
    return [_approval_response(row) for row in rows]


@router.get(
    "/channels/{channel_id}/assistant/approvals/{approval_id}",
    response_model=AssistantApprovalResponse,
)
async def get_assistant_approval(
    channel_id: int,
    approval_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    row = await AdminAgentApprovalService(session).get(
        approval_id=approval_id,
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _approval_response(row)


@router.post(
    "/channels/{channel_id}/assistant/approvals/{approval_id}/approve",
    response_model=AssistantApprovalResponse,
)
async def approve_assistant_approval(
    channel_id: int,
    approval_id: int,
    _request: AssistantApprovalDecisionRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        row = await AdminAgentApprovalService(session).approve(
            approval_id=approval_id,
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            reviewer_tg_user_id=principal.tg_user_id,
        )
    except ApprovalStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApprovalExecutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _approval_response(row)


@router.post(
    "/channels/{channel_id}/assistant/approvals/{approval_id}/reject",
    response_model=AssistantApprovalResponse,
)
async def reject_assistant_approval(
    channel_id: int,
    approval_id: int,
    _request: AssistantApprovalDecisionRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        row = await AdminAgentApprovalService(session).reject(
            approval_id=approval_id,
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            reviewer_tg_user_id=principal.tg_user_id,
        )
    except ApprovalStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _approval_response(row)


@router.post(
    "/channels/{channel_id}/assistant/series-approvals",
    response_model=AssistantSeriesApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assistant_series_approval(
    channel_id: int,
    request: AssistantSeriesApprovalCreateRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantSeriesApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        row = await AdminAgentSeriesApprovalService(session).create(
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            source_run_id=request.source_run_id,
            request_id=request.request_id,
            slots=[
                {
                    "ordinal": slot.ordinal,
                    "local_date": slot.local_date,
                    "local_time": slot.local_time,
                }
                for slot in request.slots
            ],
        )
    except SeriesApprovalInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SeriesApprovalIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if str(row.action_type) != ACTION_SCHEDULE_CONTENT_SERIES:
        raise HTTPException(status_code=409, detail="Unsupported series approval action")
    return await _series_approval_response(session, row)


@router.get(
    "/channels/{channel_id}/assistant/series-approvals",
    response_model=list[AssistantSeriesApprovalResponse],
)
async def list_assistant_series_approvals(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> list[AssistantSeriesApprovalResponse]:
    await _require_owned_channel(session, principal, channel_id)
    rows = await AdminAgentSeriesApprovalService(session).list(
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
        limit=limit,
    )
    return [await _series_approval_response(session, row) for row in rows]


@router.get(
    "/channels/{channel_id}/assistant/series-approvals/{batch_id}",
    response_model=AssistantSeriesApprovalResponse,
)
async def get_assistant_series_approval(
    channel_id: int,
    batch_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantSeriesApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    row = await AdminAgentSeriesApprovalService(session).get(
        batch_id=batch_id,
        owner_tg_user_id=principal.tg_user_id,
        channel_id=channel_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Series approval not found")
    return await _series_approval_response(session, row)


@router.post(
    "/channels/{channel_id}/assistant/series-approvals/{batch_id}/approve",
    response_model=AssistantSeriesApprovalResponse,
)
async def approve_assistant_series_approval(
    channel_id: int,
    batch_id: int,
    _request: AssistantSeriesApprovalDecisionRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantSeriesApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        row = await AdminAgentSeriesApprovalService(session).approve(
            batch_id=batch_id,
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            reviewer_tg_user_id=principal.tg_user_id,
        )
    except SeriesApprovalStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SeriesApprovalExecutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Series approval not found")
    return await _series_approval_response(session, row)


@router.post(
    "/channels/{channel_id}/assistant/series-approvals/{batch_id}/reject",
    response_model=AssistantSeriesApprovalResponse,
)
async def reject_assistant_series_approval(
    channel_id: int,
    batch_id: int,
    _request: AssistantSeriesApprovalDecisionRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantSeriesApprovalResponse:
    await _require_owned_channel(session, principal, channel_id)
    try:
        row = await AdminAgentSeriesApprovalService(session).reject(
            batch_id=batch_id,
            owner_tg_user_id=principal.tg_user_id,
            channel_id=channel_id,
            reviewer_tg_user_id=principal.tg_user_id,
        )
    except SeriesApprovalStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Series approval not found")
    return await _series_approval_response(session, row)


@router.post(
    "/channels/{channel_id}/assistant/runs",
    response_model=AssistantRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assistant_run(
    channel_id: int,
    request: AssistantRunRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantRunResponse:
    await _require_owned_channel(session, principal, channel_id)
    if request.scenario == SCENARIO_ATTENTION_TODAY:
        run = await AdminAgentRunner(session).run_attention_today(
            channel_id=channel_id,
            owner_tg_user_id=principal.tg_user_id,
        )
    elif request.scenario == SCENARIO_DRAFTS_TOMORROW:
        if not request.request_id:
            raise HTTPException(status_code=422, detail="request_id is required for drafts_tomorrow")
        run = await AdminAgentRunner(
            session,
            limits=DRAFT_SCENARIO_LIMITS,
        ).run_drafts_tomorrow(
            channel_id=channel_id,
            owner_tg_user_id=principal.tg_user_id,
            request_id=request.request_id,
        )
    elif request.scenario == SCENARIO_PREPARE_CONTENT_SERIES:
        if not request.request_id or request.operator_input is None:
            raise HTTPException(
                status_code=422,
                detail="request_id and operator_input are required for prepare_content_series",
            )
        try:
            run = await AdminAgentRunner(
                session,
                limits=SERIES_SCENARIO_LIMITS,
            ).run_prepare_content_series(
                channel_id=channel_id,
                owner_tg_user_id=principal.tg_user_id,
                request_id=request.request_id,
                brief=request.operator_input.brief,
                post_count=request.operator_input.post_count,
            )
        except AgentIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=422, detail="Unsupported assistant scenario")
    return await _response(session, run, include_events=True)


@router.post(
    "/channels/{channel_id}/assistant/runs/{run_id}/resume",
    response_model=AssistantRunResponse,
)
async def resume_assistant_run(
    channel_id: int,
    run_id: int,
    _request: AssistantResumeRequest,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantRunResponse:
    await _require_owned_channel(session, principal, channel_id)
    existing = await session.scalar(
        select(AdminAgentRun).where(
            AdminAgentRun.id == int(run_id),
            AdminAgentRun.channel_id == int(channel_id),
            AdminAgentRun.owner_tg_user_id == int(principal.tg_user_id),
        )
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Assistant run not found")
    try:
        if str(existing.scenario) == SCENARIO_DRAFTS_TOMORROW:
            run = await AdminAgentRunner(
                session,
                limits=DRAFT_SCENARIO_LIMITS,
            ).resume_drafts_tomorrow(
                channel_id=channel_id,
                owner_tg_user_id=principal.tg_user_id,
                run_id=run_id,
            )
        elif str(existing.scenario) == SCENARIO_PREPARE_CONTENT_SERIES:
            run = await AdminAgentRunner(
                session,
                limits=SERIES_SCENARIO_LIMITS,
            ).resume_prepare_content_series(
                channel_id=channel_id,
                owner_tg_user_id=principal.tg_user_id,
                run_id=run_id,
            )
        else:
            raise HTTPException(status_code=409, detail="Assistant run is not resumable")
    except AgentExecutionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentResumeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _response(session, run, include_events=True)


@router.get(
    "/channels/{channel_id}/assistant/runs",
    response_model=list[AssistantRunResponse],
)
async def list_assistant_runs(
    channel_id: int,
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[AssistantRunResponse]:
    await _require_owned_channel(session, principal, channel_id)
    rows = list(
        (
            await session.execute(
                select(AdminAgentRun)
                .where(
                    AdminAgentRun.channel_id == int(channel_id),
                    AdminAgentRun.owner_tg_user_id == int(principal.tg_user_id),
                )
                .order_by(AdminAgentRun.id.desc())
                .limit(int(limit))
            )
        ).scalars()
    )
    return [await _response(session, row, include_events=False) for row in rows]


@router.get(
    "/channels/{channel_id}/assistant/runs/{run_id}",
    response_model=AssistantRunResponse,
)
async def get_assistant_run(
    channel_id: int,
    run_id: int,
    principal: PrincipalDep,
    session: SessionDep,
) -> AssistantRunResponse:
    await _require_owned_channel(session, principal, channel_id)
    result = await session.execute(
        select(AdminAgentRun).where(
            AdminAgentRun.id == int(run_id),
            AdminAgentRun.channel_id == int(channel_id),
            AdminAgentRun.owner_tg_user_id == int(principal.tg_user_id),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Assistant run not found")
    return await _response(session, row, include_events=True)
