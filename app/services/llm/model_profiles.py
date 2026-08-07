from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

AI_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "economy": {
        "title": "⚡ Быстро",
        "description": "Быстрее и дешевле для простых черновиков.",
        "default_model": "openai/gpt-4o-mini",
        "models": {
            "from_scratch": "openai/gpt-4o-mini",
            "summary": "openai/gpt-4o-mini",
            "rewrite": "openai/gpt-4o-mini",
            "paraphrase": "openai/gpt-4o-mini",
        },
    },
    "balanced": {
        "title": "⚖️ Качественно",
        "description": "Оптимальный режим по умолчанию.",
        "default_model": "openai/gpt-4o-mini",
        "models": {
            "from_scratch": "openai/gpt-4o-mini",
            "summary": "openai/gpt-4o-mini",
            "rewrite": "openai/gpt-4o-mini",
            "paraphrase": "openai/gpt-4o-mini",
        },
    },
    "quality": {
        "title": "🧠 Максимум",
        "description": "Лучше для сложных постов, разборов и сильной редакторской правки.",
        "default_model": "anthropic/claude-3.5-sonnet",
        "models": {
            "from_scratch": "anthropic/claude-3.5-sonnet",
            "summary": "google/gemini-flash-1.5",
            "rewrite": "anthropic/claude-3.5-sonnet",
            "paraphrase": "anthropic/claude-3.5-sonnet",
        },
    },
    "custom": {
        "title": "🔧 Своя настройка",
        "description": "Индивидуальные модели для каждого режима генерации.",
        "default_model": "openai/gpt-4o-mini",
        "models": {},
    },
}


def apply_model_profile(filters: Mapping[str, Any] | None, profile: str) -> dict[str, Any]:
    """Return filters with ai_models overrides for a named quality profile."""
    if profile not in AI_MODEL_PROFILES:
        raise ValueError(f"Unknown AI model profile: {profile}")
    next_filters = dict(filters or {})
    next_filters["ai_model_profile"] = profile
    next_filters["ai_models"] = deepcopy(AI_MODEL_PROFILES[profile]["models"])
    return next_filters


def resolve_model_for_mode(ai_settings: Any, mode: str) -> str:
    """Resolve model with per-mode overrides stored in filters['ai_models']."""
    try:
        filters = dict(getattr(ai_settings, "filters", {}) or {})
        ai_models = dict(filters.get("ai_models", {}) or {})
        normalized_mode = mode.replace("-", "_")
        return ai_models.get(mode) or ai_models.get(normalized_mode) or ai_settings.model
    except Exception:
        return ai_settings.model


def get_model_profile(filters: Mapping[str, Any] | None) -> str:
    if not isinstance(filters, Mapping):
        return "custom"
    profile = str(filters.get("ai_model_profile") or "custom")
    return profile if profile in AI_MODEL_PROFILES else "custom"


def get_model_profile_menu_items() -> list[tuple[str, str]]:
    """Return ordered (code, cfg) tuples for the quality-profile menu (custom last)."""
    ordered = [code for code in ("economy", "balanced", "quality") if code in AI_MODEL_PROFILES]
    ordered.append("custom")
    return [(code, AI_MODEL_PROFILES[code]) for code in ordered]


def build_model_profile_menu_text(*, current_profile: str | None = None) -> str:
    items = get_model_profile_menu_items()
    details = next((cfg for code, cfg in items if code == current_profile), None)
    current_title = details["title"] if details else "не выбран"

    lines = [
        "🤖 Режим качества",
        "",
        f"Текущий режим: {current_title}",
        "",
        "Быстро — дешевле, подходит для черновиков и простых задач.",
        "Качественно — оптимальный режим по умолчанию.",
        "Максимум — лучший результат для сложных постов и разборов.",
        "Своя настройка — индивидуальные модели для каждого режима.",
    ]
    return "\n".join(lines)


def build_model_profile_button_rows(
    *,
    channel_id: int,
    current_profile: str | None = None,
) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for code, cfg in get_model_profile_menu_items():
        if code == "custom":
            continue
        prefix = "✅" if current_profile == code else "☑️"
        row.append((f"{prefix} {cfg['title']}", f"ai_set_model_profile_{channel_id}_{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("← Назад", f"neu_text_{channel_id}")])
    return rows


def build_model_profile_set_message(profile_code: str | None, preset_name: str | None = None) -> str:
    if preset_name:
        return f"✅ Режим: {preset_name}"
    if profile_code and profile_code in AI_MODEL_PROFILES:
        return f"✅ Режим: {AI_MODEL_PROFILES[profile_code]['title']}"
    return "✅ Режим обновлён"
