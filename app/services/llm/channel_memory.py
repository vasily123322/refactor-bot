from __future__ import annotations

from collections.abc import Mapping, Sequence
from textwrap import dedent
from typing import Any

_MEMORY_LABELS: dict[str, str] = {
    "brand": "Бренд/проект",
    "audience": "Аудитория",
    "style": "Стиль",
    "facts": "Факты",
    "forbidden": "Запрещено/избегать",
    "cta": "Постоянный CTA",
    "links": "Постоянные ссылки",
    "examples": "Примеры удачных постов",
}


MAX_CHANNEL_MEMORY_EXAMPLES = 5
MAX_CHANNEL_MEMORY_EXAMPLE_CHARS = 2000
MIN_CHANNEL_MEMORY_EXAMPLE_CHARS = 20

_MEMORY_ALIASES: dict[str, str] = {
    "бренд": "brand",
    "проект": "brand",
    "аудитория": "audience",
    "ца": "audience",
    "стиль": "style",
    "факты": "facts",
    "запрещено": "forbidden",
    "нельзя": "forbidden",
    "cta": "cta",
    "призыв": "cta",
    "ссылки": "links",
    "links": "links",
    "примеры": "examples",
    "пример": "examples",
    "examples": "examples",
}
_LIST_MEMORY_KEYS = {"facts", "forbidden", "links"}
_EDIT_MEMORY_LABELS: dict[str, str] = {
    "brand": "Бренд",
    "audience": "Аудитория",
    "style": "Стиль",
    "facts": "Факты",
    "forbidden": "Запрещено",
    "cta": "CTA",
    "links": "Ссылки",
}


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "; ".join(parts)
    return str(value).strip()


def build_channel_memory_text(memory: Mapping[str, Any] | None) -> str:
    """Format channel memory/profile into compact prompt instructions."""
    if not isinstance(memory, Mapping):
        return ""

    lines: list[str] = []
    for key, label in _MEMORY_LABELS.items():
        value = _normalize_value(memory.get(key))
        if value:
            lines.append(f"- {label}: {value}")

    if not lines:
        return ""

    return "Память канала (учитывай во всех ответах, не цитируй как отдельный блок):\n" + "\n".join(lines)


def get_channel_memory(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(filters, Mapping):
        return {}
    raw = filters.get("memory")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _split_list_value(value: str, *, split_commas: bool = True) -> list[str]:
    if split_commas:
        value = value.replace(";", ",")
        return [part.strip() for part in value.split(",") if part.strip()]
    return [part.strip() for part in value.split(";") if part.strip()]


def _flush_example(buffer: list[str], examples: list[str]) -> None:
    clean = "\n".join(line.rstrip() for line in buffer).strip()
    if clean:
        examples.append(clean)
    buffer.clear()


def parse_channel_memory_input(text: str | None) -> dict[str, str | list[str]]:
    """Parse admin-entered channel memory text into storage fields.

    Supports labeled lines (`Бренд: ...`) plus multiline examples after
    `Примеры:` separated by `---`, so real posts may contain colons and newlines.
    """

    parsed: dict[str, str | list[str]] = {}
    examples: list[str] = []
    example_buffer: list[str] = []
    collecting_examples = False

    for raw_line in dedent(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if collecting_examples and example_buffer:
                example_buffer.append("")
            continue

        if collecting_examples:
            if line == "---":
                _flush_example(example_buffer, examples)
                continue
            example_buffer.append(line)
            continue

        if ":" not in line:
            continue

        key_raw, value_raw = line.split(":", 1)
        key = _MEMORY_ALIASES.get(key_raw.strip().lower())
        value = value_raw.strip()
        if not key:
            continue

        if key == "examples":
            if value:
                examples.extend(_split_list_value(value, split_commas=False))
            else:
                collecting_examples = True
            continue

        if not value:
            continue
        if key in _LIST_MEMORY_KEYS:
            parsed[key] = _split_list_value(value)
        else:
            parsed[key] = value

    if collecting_examples:
        _flush_example(example_buffer, examples)
    if examples:
        parsed["examples"] = examples[-MAX_CHANNEL_MEMORY_EXAMPLES:]
    return parsed


def build_channel_memory_edit_template(memory: Mapping[str, Any] | None) -> str:
    """Build a copy-editable template with current channel memory prefilled."""

    memory = dict(memory or {}) if isinstance(memory, Mapping) else {}

    def line(key: str, fallback: str = "") -> str:
        value = _normalize_value(memory.get(key)) or fallback
        return f"{_EDIT_MEMORY_LABELS[key]}: {value}"

    lines = [
        line("brand", "Yuby"),
        line("audience", "владельцы Telegram-каналов"),
        line("style", "коротко, уверенно, без канцелярита"),
        line("facts", "бот помогает вести каналы; есть автопостинг"),
        line("forbidden", "гарантированный доход; непроверенные факты"),
        line("cta", "Напишите админу для подключения"),
        line("links", "https://example.com"),
    ]
    examples = [str(item).strip() for item in (memory.get("examples") or []) if str(item).strip()]
    if examples:
        lines.append("Примеры:")
        for example in examples:
            lines.append("---")
            lines.append(example)
    else:
        lines.append("Примеры: короткий пример удачного поста; ещё один пример")
    return "\n".join(lines)


def clear_channel_memory(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    next_filters = dict(filters or {})
    next_filters.pop("memory", None)
    return next_filters


def upsert_channel_memory(filters: Mapping[str, Any] | None, **updates: Any) -> dict[str, Any]:
    next_filters = dict(filters or {})
    memory = get_channel_memory(next_filters)
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
        if value in ("", [], {}):
            memory.pop(key, None)
        else:
            memory[key] = value
    if memory:
        next_filters["memory"] = memory
    else:
        next_filters.pop("memory", None)
    return next_filters


def append_channel_memory_example(
    filters: Mapping[str, Any] | None,
    example: str | None,
    *,
    limit: int = MAX_CHANNEL_MEMORY_EXAMPLES,
    max_chars: int = MAX_CHANNEL_MEMORY_EXAMPLE_CHARS,
    min_chars: int = MIN_CHANNEL_MEMORY_EXAMPLE_CHARS,
) -> dict[str, Any]:
    """Append a good-post example to channel memory, preserving unrelated filters."""

    clean = (example or "").strip()
    if len(clean) < max(0, int(min_chars)):
        return dict(filters or {})

    clean = clean[: max(1, int(max_chars))]
    memory = get_channel_memory(filters)
    examples = [str(item).strip() for item in (memory.get("examples") or []) if str(item).strip()]
    examples = [item for item in examples if item != clean]
    examples.append(clean)
    examples = examples[-max(1, int(limit)) :]
    return upsert_channel_memory(filters, examples=examples)
