from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


CAPABILITY_READ_ONLY = "read_only"
CAPABILITY_DRAFT_WRITE = "draft_write"

RESUME_NONE = "none"
RESUME_EXPLICIT = "explicit"

CONTEXT_NONE = "none"
CONTEXT_EDITORIAL_V1 = "editorial_v1"

APPROVAL_NONE = "none"
APPROVAL_EXPLICIT = "explicit"


def _freeze_json(value: Any) -> Any:
    if isinstance(value, MappingABC):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class AdminAgentSkillSpec:
    skill_id: str
    version: str
    scenario: str
    execution_limits: Mapping[str, int | float]
    allowed_capability_classes: tuple[str, ...]
    context_profile: str
    resume_policy: str
    display_title: str
    description: str
    category: str
    operator_input_schema: Mapping[str, Any]
    result_kind: str
    approval_requirement: str
    capability_summary: str
    context_requirements: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_limits",
            _freeze_json(dict(self.execution_limits)),
        )
        object.__setattr__(
            self,
            "operator_input_schema",
            _freeze_json(dict(self.operator_input_schema)),
        )


class AdminAgentSkillRegistry:
    def __init__(self, specs: tuple[AdminAgentSkillSpec, ...]):
        self._by_version: dict[tuple[str, str], AdminAgentSkillSpec] = {}
        self._current_by_scenario: dict[str, AdminAgentSkillSpec] = {}
        expected_capabilities = {
            "attention_today": {CAPABILITY_READ_ONLY},
            "drafts_tomorrow": {CAPABILITY_DRAFT_WRITE},
            "prepare_content_series": {CAPABILITY_DRAFT_WRITE},
        }
        allowed_profiles = {CONTEXT_NONE, CONTEXT_EDITORIAL_V1}
        allowed_resume = {RESUME_NONE, RESUME_EXPLICIT}
        allowed_approval = {APPROVAL_NONE, APPROVAL_EXPLICIT}

        for spec in specs:
            key = (spec.skill_id, str(spec.version))
            if key in self._by_version:
                raise ValueError(f"duplicate admin-agent skill version: {key}")
            if spec.scenario not in expected_capabilities:
                raise ValueError(f"unsupported admin-agent skill scenario: {spec.scenario}")
            capabilities = set(spec.allowed_capability_classes)
            if capabilities != expected_capabilities[spec.scenario]:
                raise ValueError(
                    f"unexpected capability classes for {spec.scenario}: {sorted(capabilities)}"
                )
            if spec.context_profile not in allowed_profiles:
                raise ValueError(f"unsupported admin-agent context profile: {spec.context_profile}")
            if spec.resume_policy not in allowed_resume:
                raise ValueError(f"unsupported admin-agent resume policy: {spec.resume_policy}")
            if spec.approval_requirement not in allowed_approval:
                raise ValueError(
                    f"unsupported admin-agent approval requirement: {spec.approval_requirement}"
                )
            if spec.scenario == "attention_today" and spec.resume_policy != RESUME_NONE:
                raise ValueError("attention_today must remain non-resumable")
            if (
                spec.scenario in {"drafts_tomorrow", "prepare_content_series"}
                and spec.resume_policy != RESUME_EXPLICIT
            ):
                raise ValueError(f"{spec.scenario} must use explicit resume")
            if (
                spec.scenario in {"drafts_tomorrow", "prepare_content_series"}
                and spec.context_profile != CONTEXT_EDITORIAL_V1
            ):
                raise ValueError(f"{spec.scenario} must use bounded editorial context")
            if not spec.display_title.strip() or not spec.description.strip():
                raise ValueError("admin-agent presentation title/description must be non-empty")
            if not spec.category.strip() or not spec.result_kind.strip():
                raise ValueError("admin-agent category/result kind must be non-empty")
            if not spec.capability_summary.strip() or not spec.context_requirements.strip():
                raise ValueError(
                    "admin-agent capability/context presentation metadata must be non-empty"
                )
            schema = spec.operator_input_schema
            if (
                schema.get("type") != "object"
                or schema.get("additionalProperties") is not False
                or not isinstance(schema.get("properties"), MappingABC)
            ):
                raise ValueError("admin-agent operator input schema must be a closed object schema")
            self._by_version[key] = spec
            if spec.scenario in self._current_by_scenario:
                raise ValueError(f"multiple current skill versions for {spec.scenario}")
            self._current_by_scenario[spec.scenario] = spec

    def current_for_scenario(self, scenario: str) -> AdminAgentSkillSpec:
        try:
            return self._current_by_scenario[str(scenario)]
        except KeyError as exc:
            raise KeyError(f"unknown admin-agent scenario: {scenario}") from exc

    def resolve(self, skill_id: str | None, version: str | None) -> AdminAgentSkillSpec:
        if not skill_id or not version:
            raise KeyError("admin-agent run has no durable skill version")
        key = (str(skill_id), str(version))
        try:
            return self._by_version[key]
        except KeyError as exc:
            raise KeyError(f"unknown admin-agent skill version: {key[0]}@{key[1]}") from exc

    @property
    def specs(self) -> tuple[AdminAgentSkillSpec, ...]:
        return tuple(self._by_version.values())

    @property
    def current_specs(self) -> tuple[AdminAgentSkillSpec, ...]:
        return tuple(self._current_by_scenario.values())


_EMPTY_OPERATOR_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_CONTENT_SERIES_OPERATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "brief": {"type": "string", "minLength": 20, "maxLength": 2000},
        "post_count": {"type": "integer", "minimum": 2, "maximum": 8},
    },
    "required": ["brief", "post_count"],
    "additionalProperties": False,
}


SKILL_REGISTRY = AdminAgentSkillRegistry(
    (
        AdminAgentSkillSpec(
            skill_id="attention_today",
            version="1",
            scenario="attention_today",
            execution_limits={
                "max_steps": 5,
                "max_tool_calls": 4,
                "max_llm_calls": 1,
                "max_seconds": 20.0,
                "max_items_per_tool": 20,
            },
            allowed_capability_classes=(CAPABILITY_READ_ONLY,),
            context_profile=CONTEXT_NONE,
            resume_policy=RESUME_NONE,
            display_title="Что сегодня требует внимания?",
            description="Собирает bounded operational snapshot и приоритизирует проверяемые факты.",
            category="operations",
            operator_input_schema=_EMPTY_OPERATOR_SCHEMA,
            result_kind="attention_brief",
            approval_requirement=APPROVAL_NONE,
            capability_summary="Read-only: состояние канала без Content или scheduling writes.",
            context_requirements="Channel-scoped operational state; editorial context не требуется.",
        ),
        AdminAgentSkillSpec(
            skill_id="drafts_tomorrow",
            version="1",
            scenario="drafts_tomorrow",
            execution_limits={
                "max_steps": 4,
                "max_tool_calls": 0,
                "max_llm_calls": 1,
                "max_seconds": 30.0,
                "max_items_per_tool": 0,
            },
            allowed_capability_classes=(CAPABILITY_DRAFT_WRITE,),
            context_profile=CONTEXT_EDITORIAL_V1,
            resume_policy=RESUME_EXPLICIT,
            display_title="Создать 3 черновика на завтра",
            description="Готовит ровно три ordinary Content drafts в стиле и контексте канала.",
            category="editorial",
            operator_input_schema=_EMPTY_OPERATOR_SCHEMA,
            result_kind="content_drafts",
            approval_requirement=APPROVAL_NONE,
            capability_summary=(
                "Draft-write: создаёт bounded Content drafts; scheduling остаётся отдельным approval."
            ),
            context_requirements=(
                "Existing channel memory/profile + bounded recent and scheduled Content context."
            ),
        ),
        AdminAgentSkillSpec(
            skill_id="prepare_content_series",
            version="1",
            scenario="prepare_content_series",
            execution_limits={
                "max_steps": 4,
                "max_tool_calls": 0,
                "max_llm_calls": 1,
                "max_seconds": 30.0,
                "max_items_per_tool": 0,
            },
            allowed_capability_classes=(CAPABILITY_DRAFT_WRITE,),
            context_profile=CONTEXT_EDITORIAL_V1,
            resume_policy=RESUME_EXPLICIT,
            display_title="Подготовить серию постов",
            description=(
                "Строит bounded series plan и создаёт указанное число ordinary Content drafts."
            ),
            category="editorial",
            operator_input_schema=_CONTENT_SERIES_OPERATOR_SCHEMA,
            result_kind="content_series",
            approval_requirement=APPROVAL_NONE,
            capability_summary=(
                "Draft-write: создаёт только Content drafts; scheduling/publishing не выполняются."
            ),
            context_requirements=(
                "Existing channel memory/profile + bounded recent and scheduled Content context."
            ),
        ),
    )
)
