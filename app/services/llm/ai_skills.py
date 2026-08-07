from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_EMOJI_RE = re.compile(
    "[\U0001f1e6-\U0001f1ff]|[\U0001f300-\U0001f5ff]|[\U0001f600-\U0001f64f]|"
    "[\U0001f680-\U0001f6ff]|[\U0001f700-\U0001f77f]|[\U0001f780-\U0001f7ff]|"
    "[\U0001f800-\U0001f8ff]|[\U0001f900-\U0001f9ff]|[\U0001fa00-\U0001faff]|"
    "[\u2600-\u26ff]|[\u2700-\u27bf]|[\u2b00-\u2bff]|\u200d|\ufe0f"
)


def normalize_ai_skill_title(title: str | None) -> str:
    clean = _EMOJI_RE.sub("", title or "").strip()
    return clean or "Навык ИИ"


def _preset_id(preset: Any) -> int | None:
    try:
        return int(getattr(preset, "id"))
    except Exception:
        return None


def _description(preset: Any, *, limit: int = 110) -> str:
    text = str(getattr(preset, "description", "") or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_ai_skills_menu_text(presets: Sequence[Any], *, current_preset_id: int | None = None) -> str:
    current = next((preset for preset in presets if _preset_id(preset) == current_preset_id), None)
    current_title = normalize_ai_skill_title(getattr(current, "title", "")) if current else "не выбран"

    lines = [
        "🧠 Навыки ИИ",
        "",
        "Навык задаёт сценарий генерации: новость, рерайт, саммари, промо, экспертный пост и т.д.",
        f"Текущий навык: {current_title}",
    ]
    if presets:
        lines.extend(["", "Доступные навыки:"])
        for preset in presets:
            preset_id = _preset_id(preset)
            prefix = "✅" if preset_id == current_preset_id else "☑️"
            title = normalize_ai_skill_title(getattr(preset, "title", ""))
            desc = _description(preset)
            lines.append(f"{prefix} {title}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


def build_ai_skill_button_rows(
    presets: Sequence[Any],
    *,
    channel_id: int,
    current_preset_id: int | None = None,
) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for preset in presets:
        preset_id = _preset_id(preset)
        if preset_id is None:
            continue
        prefix = "✅" if preset_id == current_preset_id else "☑️"
        label = f"{prefix} {normalize_ai_skill_title(getattr(preset, 'title', ''))}"
        row.append((label, f"ai_set_preset_{channel_id}_{preset_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("← Назад", f"neu_text_{channel_id}")])
    return rows


def build_ai_skill_set_message(preset: Any | None) -> str:
    if preset is None:
        return "✅ Навык ИИ выбран"
    return f"✅ Навык ИИ: {normalize_ai_skill_title(getattr(preset, 'title', ''))}"
