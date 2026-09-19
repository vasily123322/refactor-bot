from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


CAPABILITY_READ_ONLY = "read_only"
CAPABILITY_DRAFT_WRITE = "draft_write"

RESUME_NONE = "none"
RESUME_EXPLICIT = "explicit"

CONTEXT_NONE = "none"
CONTEXT_EDITORIAL_V1 = "editorial_v1"


@dataclass(frozen=True, slots=True)
class AdminAgentSkillSpec:
    skill_id: str
    version: str
    scenario: str
    execution_limits: Mapping[str, int | float]
    allowed_capability_classes: tuple[str, ...]
    context_profile: str
    resume_policy: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_limits",
            MappingProxyType(dict(self.execution_limits)),
        )


class AdminAgentSkillRegistry:
    def __init__(self, specs: tuple[AdminAgentSkillSpec, ...]):
        self._by_version: dict[tuple[str, str], AdminAgentSkillSpec] = {}
        self._current_by_scenario: dict[str, AdminAgentSkillSpec] = {}
        expected_capabilities = {
            "attention_today": {CAPABILITY_READ_ONLY},
            "drafts_tomorrow": {CAPABILITY_DRAFT_WRITE},
        }
        allowed_profiles = {CONTEXT_NONE, CONTEXT_EDITORIAL_V1}
        allowed_resume = {RESUME_NONE, RESUME_EXPLICIT}

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
            if spec.scenario == "attention_today" and spec.resume_policy != RESUME_NONE:
                raise ValueError("attention_today must remain non-resumable")
            if spec.scenario == "drafts_tomorrow" and spec.resume_policy != RESUME_EXPLICIT:
                raise ValueError("drafts_tomorrow must use explicit resume")
            if spec.scenario == "drafts_tomorrow" and spec.context_profile != CONTEXT_EDITORIAL_V1:
                raise ValueError("drafts_tomorrow must use bounded editorial context")
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
        ),
    )
)
