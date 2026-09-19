from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import to_user_tz
from app.domain.admin_agent import AdminAgentEvent, AdminAgentRun
from app.domain.publishing.models import Publication, ScheduleEntry
from app.domain.sources.models import SourceConnector
from app.services.ai_activity import AIActivityService
from app.services.ai_generation import AIGenerationService


SCENARIO_ATTENTION_TODAY = "attention_today"

RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_AWAITING_APPROVAL = "awaiting_approval"  # Reserved for later product slices.

SIDE_EFFECT_READ_ONLY = "read_only"
SIDE_EFFECT_APPROVAL_REQUIRED = "approval_required"  # Reserved; never registered in MVP A.

DEFAULT_ATTENTION_TIMEZONE = "UTC+3"
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SAFE_SUMMARY_MAX_CHARS = 1200
_PROVIDER_QUOTA_ERRORS = (
    "Превышен дневной лимит токенов",
    "Превышен месячный лимит токенов",
)


class AgentExecutionError(RuntimeError):
    pass


class AgentExecutionLimit(AgentExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_steps: int = 5
    max_tool_calls: int = 4
    max_llm_calls: int = 1
    max_seconds: float = 20.0
    max_items_per_tool: int = 20


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    session: AsyncSession
    channel_id: int
    owner_tg_user_id: int
    now_utc: datetime
    limit: int


ToolExecutor = Callable[[AgentToolContext], Awaitable[dict]]


@dataclass(frozen=True, slots=True)
class AgentToolSpec:
    name: str
    side_effect: str
    execute: ToolExecutor


class BoundedToolRegistry:
    def __init__(self, specs: tuple[AgentToolSpec, ...]):
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("duplicate admin-agent tool name")
        if any(spec.side_effect != SIDE_EFFECT_READ_ONLY for spec in specs):
            raise ValueError("MVP A may register read-only tools only")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, name: str) -> AgentToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise AgentExecutionError(f"tool is not allowlisted: {name}") from exc


async def _resolve_attention_timezone(session: AsyncSession, channel_id: int) -> str:
    result = await session.execute(
        select(ScheduleEntry.timezone)
        .where(
            ScheduleEntry.channel_id == int(channel_id),
            ScheduleEntry.timezone.is_not(None),
        )
        .order_by(ScheduleEntry.created_at.desc(), ScheduleEntry.id.desc())
        .limit(1)
    )
    value = result.scalar_one_or_none()
    return str(value).strip() if value and str(value).strip() else DEFAULT_ATTENTION_TIMEZONE


def _today_bounds(now_utc: datetime, timezone_name: str) -> tuple[datetime, datetime]:
    local_now = to_user_tz(now_utc, timezone_name)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _ref(**values: int | None) -> dict[str, int]:
    return {key: int(value) for key, value in values.items() if value is not None}


async def _schedule_attention(ctx: AgentToolContext) -> dict:
    timezone_name = await _resolve_attention_timezone(ctx.session, ctx.channel_id)
    start_utc, end_utc = _today_bounds(ctx.now_utc, timezone_name)
    rows = (
        await ctx.session.execute(
            select(ScheduleEntry, Publication)
            .outerjoin(Publication, Publication.schedule_entry_id == ScheduleEntry.id)
            .where(
                ScheduleEntry.channel_id == int(ctx.channel_id),
                ScheduleEntry.status == "pending",
                ScheduleEntry.scheduled_at < end_utc,
            )
            .order_by(ScheduleEntry.scheduled_at.asc(), ScheduleEntry.id.asc())
            .limit(ctx.limit)
        )
    ).all()

    facts: list[dict] = []
    for schedule, publication in rows:
        overdue = schedule.scheduled_at < start_utc
        facts.append(
            {
                "fact_id": f"schedule:{int(schedule.id)}:{'overdue' if overdue else 'today'}",
                "category": "schedule",
                "severity": "high" if overdue else "info",
                "title": (
                    "Просрочена ожидающая публикация"
                    if overdue
                    else "Сегодня запланирована публикация"
                ),
                "detail": (
                    "ScheduleEntry остаётся pending после начала текущего дня."
                    if overdue
                    else "ScheduleEntry pending и попадает в текущий день канала."
                ),
                "refs": _ref(
                    schedule_entry_id=schedule.id,
                    publication_id=(publication.id if publication is not None else None),
                    content_item_id=schedule.content_item_id,
                ),
                "suggested_action": "Проверьте запись в Планере и её canonical Publication.",
            }
        )
    return {
        "timezone": timezone_name,
        "today_start_utc": start_utc.isoformat(),
        "today_end_utc": end_utc.isoformat(),
        "facts": facts,
    }


async def _publication_attention(ctx: AgentToolContext) -> dict:
    rows = list(
        (
            await ctx.session.execute(
                select(Publication)
                .where(
                    Publication.channel_id == int(ctx.channel_id),
                    or_(
                        Publication.status.in_(("failed", "error")),
                        Publication.last_error.is_not(None),
                    ),
                )
                .order_by(Publication.updated_at.desc(), Publication.id.desc())
                .limit(ctx.limit)
            )
        ).scalars()
    )
    facts = [
        {
            "fact_id": f"publication:{int(row.id)}:failure",
            "category": "publication",
            "severity": "high",
            "title": "Publication требует проверки",
            "detail": (
                f"Canonical Publication status={row.status}; сохранена ошибка доставки."
                if row.last_error
                else f"Canonical Publication status={row.status}."
            ),
            "refs": _ref(
                publication_id=row.id,
                schedule_entry_id=row.schedule_entry_id,
                content_item_id=row.content_item_id,
            ),
            "suggested_action": "Откройте Планер и проверьте canonical delivery state перед повторным действием.",
        }
        for row in rows
    ]
    return {"facts": facts}


async def _source_health(ctx: AgentToolContext) -> dict:
    rows = list(
        (
            await ctx.session.execute(
                select(SourceConnector)
                .where(
                    SourceConnector.channel_id == int(ctx.channel_id),
                    SourceConnector.enabled.is_(True),
                    SourceConnector.status.in_(("broken", "auth_required", "degraded")),
                )
                .order_by(SourceConnector.id.asc())
                .limit(ctx.limit)
            )
        ).scalars()
    )
    facts: list[dict] = []
    for row in rows:
        status = str(row.status)
        facts.append(
            {
                "fact_id": f"source:{int(row.id)}:{status}",
                "category": "source",
                "severity": "high" if status in {"broken", "auth_required"} else "medium",
                "title": "Источник требует внимания",
                "detail": (
                    f"SourceConnector status={status}"
                    + (f": {str(row.status_reason)[:240]}" if row.status_reason else ".")
                ),
                "refs": _ref(source_connector_id=row.id),
                "suggested_action": "Откройте Источники и запустите доступную диагностику/проверьте авторизацию.",
            }
        )
    return {"facts": facts}


def _quota_fact(
    *,
    period: str,
    used: int,
    limit: int | None,
) -> dict | None:
    if limit is None or limit <= 0:
        return None
    ratio = used / limit
    if ratio < 0.8:
        return None
    exhausted = used >= limit
    return {
        "fact_id": f"ai:quota:{period}",
        "category": "ai",
        "severity": "high" if exhausted else "medium",
        "title": "AI quota исчерпана" if exhausted else "AI quota близка к лимиту",
        "detail": f"{period}: использовано {used} из {limit} токенов.",
        "refs": {},
        "suggested_action": "Проверьте AI usage/лимиты в AI Studio.",
    }


async def _ai_health(ctx: AgentToolContext) -> dict:
    snapshot = await AIActivityService(ctx.session).snapshot(channel_id=ctx.channel_id, limit=1)
    usage = snapshot.usage
    facts: list[dict] = []
    if not usage.configured:
        facts.append(
            {
                "fact_id": "ai:not_configured",
                "category": "ai",
                "severity": "medium",
                "title": "Channel AI не настроен",
                "detail": "Для канала отсутствует ChannelAISettings.",
                "refs": {},
                "suggested_action": "Проверьте настройки AI канала.",
            }
        )
    elif not usage.enabled:
        facts.append(
            {
                "fact_id": "ai:disabled",
                "category": "ai",
                "severity": "medium",
                "title": "Channel AI выключен",
                "detail": "ChannelAISettings.enabled=false.",
                "refs": {},
                "suggested_action": "Включайте AI только если он нужен для этого workflow.",
            }
        )
    for period, used, limit in (
        ("day", usage.tokens_used_day, usage.tokens_limit_day),
        ("month", usage.tokens_used_month, usage.tokens_limit_month),
    ):
        fact = _quota_fact(period=period, used=used, limit=limit)
        if fact is not None:
            facts.append(fact)
    return {
        "ai_configured": usage.configured,
        "ai_enabled": usage.enabled,
        "model": usage.model,
        "facts": facts,
    }


ATTENTION_TOOLS = BoundedToolRegistry(
    (
        AgentToolSpec("schedule_attention", SIDE_EFFECT_READ_ONLY, _schedule_attention),
        AgentToolSpec("publication_attention", SIDE_EFFECT_READ_ONLY, _publication_attention),
        AgentToolSpec("source_health", SIDE_EFFECT_READ_ONLY, _source_health),
        AgentToolSpec("ai_health", SIDE_EFFECT_READ_ONLY, _ai_health),
    )
)


def _fallback_summary(facts: list[dict]) -> str:
    actionable = [fact for fact in facts if fact.get("severity") != "info"]
    if not actionable:
        if facts:
            return "Критичных operational предупреждений не найдено; запланированные на сегодня записи показаны ниже."
        return "На сегодня явных operational предупреждений в проверяемом snapshot не найдено."
    categories = sorted({str(fact.get("category") or "") for fact in actionable if fact.get("category")})
    suffix = f" Категории: {', '.join(categories)}." if categories else ""
    return f"Требуют внимания {len(actionable)} operational пунктов.{suffix}"


def _safe_llm_summary(text: str, facts: list[dict]) -> str | None:
    clean = " ".join(str(text or "").split()).strip()
    if not clean or len(clean) > _SAFE_SUMMARY_MAX_CHARS:
        return None
    # Object identifiers, dates and numeric claims must come from deterministic facts,
    # not from the formatter. Keep the summary number-free and leave all IDs in refs.
    if re.search(r"\d", clean) or "http://" in clean.lower() or "https://" in clean.lower():
        return None
    return clean


class AdminAgentRunner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        limits: AgentLimits | None = None,
        registry: BoundedToolRegistry = ATTENTION_TOOLS,
        now_utc: datetime | None = None,
    ):
        self.session = session
        self.limits = limits or AgentLimits()
        self.registry = registry
        self.now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._sequence = 0
        self._steps = 0
        self._tool_calls = 0
        self._llm_calls = 0

    def _step(self) -> None:
        self._steps += 1
        if self._steps > self.limits.max_steps:
            raise AgentExecutionLimit("admin agent step limit exceeded")

    async def _event(
        self,
        run: AdminAgentRun,
        event_type: str,
        *,
        tool_name: str | None = None,
        payload: dict | None = None,
    ) -> None:
        self._sequence += 1
        self.session.add(
            AdminAgentEvent(
                run_id=int(run.id),
                sequence=self._sequence,
                event_type=event_type,
                tool_name=tool_name,
                payload=dict(payload or {}),
            )
        )
        await self.session.commit()

    async def _execute(self, run: AdminAgentRun) -> dict:
        ctx = AgentToolContext(
            session=self.session,
            channel_id=int(run.channel_id),
            owner_tg_user_id=int(run.owner_tg_user_id),
            now_utc=self.now_utc,
            limit=max(1, min(int(self.limits.max_items_per_tool), 100)),
        )
        outputs: dict[str, dict] = {}
        facts: list[dict] = []

        for name in self.registry.names:
            self._step()
            self._tool_calls += 1
            if self._tool_calls > self.limits.max_tool_calls:
                raise AgentExecutionLimit("admin agent tool-call limit exceeded")
            spec = self.registry.get(name)
            await self._event(
                run,
                "tool_started",
                tool_name=name,
                payload={"side_effect": spec.side_effect},
            )
            output = await spec.execute(ctx)
            outputs[name] = output
            tool_facts = list(output.get("facts") or [])
            facts.extend(tool_facts)
            await self._event(
                run,
                "tool_finished",
                tool_name=name,
                payload={"fact_count": len(tool_facts)},
            )

        facts.sort(
            key=lambda fact: (
                _SEVERITY_ORDER.get(str(fact.get("severity")), 99),
                str(fact.get("category") or ""),
                str(fact.get("fact_id") or ""),
            )
        )

        timezone_name = str(outputs.get("schedule_attention", {}).get("timezone") or DEFAULT_ATTENTION_TIMEZONE)
        ai_meta = outputs.get("ai_health", {})
        summary = _fallback_summary(facts)
        generated_by = "deterministic_fallback"
        model = ai_meta.get("model")
        tokens_used = 0

        ai_enabled = bool(ai_meta.get("ai_configured") and ai_meta.get("ai_enabled"))
        quota_exhausted = any(
            fact.get("fact_id") in {"ai:quota:day", "ai:quota:month"}
            and fact.get("severity") == "high"
            for fact in facts
        )
        if ai_enabled and not quota_exhausted and facts:
            self._step()
            self._llm_calls += 1
            if self._llm_calls > self.limits.max_llm_calls:
                raise AgentExecutionLimit("admin agent LLM-call limit exceeded")
            await self._event(run, "model_started", payload={"model": model})
            prompt_payload = {
                "timezone": timezone_name,
                "facts": facts,
            }
            result = await AIGenerationService(self.session).run_pipeline(
                channel_id=int(run.channel_id),
                mode="from_scratch",
                topic=(
                    "Сформулируй краткое operational summary на русском в 1–2 предложениях. "
                    "Используй ТОЛЬКО переданные facts. Не добавляй факты, числа, ID, даты, "
                    "ссылки, причины или состояния, которых нет во входе. Не давай команд "
                    "на публикацию и не меняй данные.\nFACTS_JSON:\n"
                    + json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"))
                ),
                extra={"force_custom": False},
            )
            if not result.get("success"):
                error = str(result.get("error") or "")
                await self._event(run, "model_finished", payload={"success": False})
                if any(marker in error for marker in _PROVIDER_QUOTA_ERRORS):
                    generated_by = "deterministic_fallback"
                else:
                    raise AgentExecutionError("AI provider failed while formatting the bounded brief")
            else:
                tokens_used = int(result.get("tokens_used") or 0)
                safe_summary = _safe_llm_summary(str(result.get("text") or ""), facts)
                if safe_summary is not None:
                    summary = safe_summary
                    generated_by = "llm"
                await self._event(
                    run,
                    "model_finished",
                    payload={"success": True, "tokens_used": tokens_used},
                )

        return {
            "scenario": SCENARIO_ATTENTION_TODAY,
            "summary": summary,
            "attention_items": facts,
            "timezone": timezone_name,
            "generated_by": generated_by,
            "tool_names": list(self.registry.names),
            "execution_limits": {
                "max_steps": self.limits.max_steps,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_llm_calls": self.limits.max_llm_calls,
                "max_seconds": self.limits.max_seconds,
            },
            "_model": model,
            "_tokens_used": tokens_used,
        }

    async def run_attention_today(
        self,
        *,
        channel_id: int,
        owner_tg_user_id: int,
    ) -> AdminAgentRun:
        run = AdminAgentRun(
            owner_tg_user_id=int(owner_tg_user_id),
            channel_id=int(channel_id),
            scenario=SCENARIO_ATTENTION_TODAY,
            status=RUN_RUNNING,
            started_at=self.now_utc,
        )
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        await self._event(
            run,
            "run_started",
            payload={"scenario": SCENARIO_ATTENTION_TODAY},
        )

        try:
            result = await asyncio.wait_for(
                self._execute(run),
                timeout=max(0.01, float(self.limits.max_seconds)),
            )
            run.model = result.pop("_model", None)
            run.tokens_used = int(result.pop("_tokens_used", 0) or 0)
            run.result = result
            run.status = RUN_COMPLETED
            run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
            await self._event(
                run,
                "run_completed",
                payload={
                    "attention_count": len(result.get("attention_items") or []),
                    "generated_by": result.get("generated_by"),
                },
            )
        except asyncio.TimeoutError:
            await self.session.rollback()
            run = await self.session.get(AdminAgentRun, int(run.id))
            assert run is not None
            run.status = RUN_FAILED
            run.error = "admin agent wall-clock limit exceeded"
            run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
            await self._event(run, "run_failed", payload={"reason": "wall_clock_limit"})
        except AgentExecutionLimit as exc:
            await self.session.rollback()
            run = await self.session.get(AdminAgentRun, int(run.id))
            assert run is not None
            run.status = RUN_FAILED
            run.error = str(exc)
            run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
            await self._event(run, "run_failed", payload={"reason": "execution_limit"})
        except Exception:
            await self.session.rollback()
            run = await self.session.get(AdminAgentRun, int(run.id))
            assert run is not None
            run.status = RUN_FAILED
            run.error = "admin agent execution failed"
            run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
            await self._event(run, "run_failed", payload={"reason": "execution_error"})

        await self.session.refresh(run)
        return run
