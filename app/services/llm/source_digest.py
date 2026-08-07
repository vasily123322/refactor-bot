from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

MAX_SOURCE_ITEM_CHARS = 1800
MAX_DIGEST_CONTEXT_CHARS = 7000

SOURCE_DIGEST_MODE_LABELS = {
    "summary": "саммари",
    "rewrite": "рерайт",
    "paraphrase": "перефраз",
    "custom": "свой режим",
}


def normalize_source_digest_mode(mode: str | None) -> str:
    mode_key = (mode or "summary").strip().lower()
    return mode_key if mode_key in SOURCE_DIGEST_MODE_LABELS else "custom"


def clean_source_text(text: str | None, *, limit: int = MAX_SOURCE_ITEM_CHARS) -> str:
    """Normalize one source item for digest prompt context."""
    clean = " ".join((text or "").split())
    if len(clean) > limit:
        clean = clean[: max(0, limit - 1)].rstrip() + "…"
    return clean


def build_source_digest_context(items: Iterable[Mapping[str, Any]]) -> str:
    """Build compact multi-source context for LLM digest generation."""
    blocks: list[str] = []
    total_len = 0
    for idx, item in enumerate(items, start=1):
        source = str(item.get("source") or "Источник").strip()
        url = str(item.get("url") or "").strip()
        text = clean_source_text(str(item.get("text") or ""))
        if not text:
            continue
        header = f"Источник {idx}: {source}"
        if url:
            header += f" ({url})"
        meta: list[str] = []
        mode = normalize_source_digest_mode(str(item.get("mode") or "summary"))
        meta.append(f"Режим: {SOURCE_DIGEST_MODE_LABELS[mode]}")
        if bool(item.get("citation_enabled", False)):
            meta.append("Можно дать ссылку/упоминание источника")
        block = f"{header}\n{' · '.join(meta)}\n{text}"
        if total_len + len(block) > MAX_DIGEST_CONTEXT_CHARS:
            break
        blocks.append(block)
        total_len += len(block)
    return "\n\n---\n\n".join(blocks)


def summarize_source_digest_modes(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    modes: dict[str, int] = {}
    citation_count = 0
    for item in items:
        mode = normalize_source_digest_mode(str(item.get("mode") or "summary"))
        modes[mode] = modes.get(mode, 0) + 1
        if bool(item.get("citation_enabled", False)):
            citation_count += 1
    return {"modes": modes, "citation_count": citation_count}


def _plural_items(count: int) -> str:
    """Return correct Russian plural form for 'материал'."""
    n = abs(count) % 100
    if 11 <= n <= 19:
        return f"{count} материалов"
    last = n % 10
    if last == 1:
        return f"{count} материала"
    if 2 <= last <= 4:
        return f"{count} материалов"
    return f"{count} материалов"


_VARIANT_INSTRUCTIONS: dict[str, str] = {
    "analysis": "Сделай разбор: выдели 3–5 ключевых пунктов, добавь контекст и выводы. Структура: хук → пункты → вывод.",
    "news": "Сделай новостной пост: факт → почему важно → что дальше. Коротко, без воды, с конкретикой.",
    "sales": "Сделай продающий пост: проблема → решение → выгоды → мягкий CTA. Без обещаний результата.",
    "meme": "Сделай лёгкий/мемный пост: коротко, живо, иронично, но без токсичности.",
}


def build_source_digest_instruction(
    item_count: int,
    *,
    modes: Mapping[str, int] | None = None,
    citation_count: int = 0,
    variant: str | None = None,
) -> str:
    """Instruction for turning source snippets into a draft post."""

    item_label = _plural_items(item_count)
    parts = [
        f"На основе {item_label} из источников канала сделай один готовый Telegram-пост.",
        "Выбери главное, объедини пересекающиеся факты, не выдумывай деталей, не копируй формулировки источников.",
        "Если фактов мало — честно сделай короткий нейтральный пост.",
    ]
    variant_key = (variant or "").strip().lower()
    variant_instruction = _VARIANT_INSTRUCTIONS.get(variant_key, "")
    if variant_instruction:
        parts.append(variant_instruction)
    if modes:
        mode_bits = []
        for key in ("summary", "rewrite", "paraphrase", "custom"):
            count = int(modes.get(key, 0) or 0)
            if count:
                mode_bits.append(f"{SOURCE_DIGEST_MODE_LABELS[key]}: {count}")
        if mode_bits:
            parts.append("Учитывай режимы источников: " + ", ".join(mode_bits) + ".")
    if citation_count:
        parts.append(
            f"Цитирование включено у {citation_count} источников: если используешь их конкретные факты, можно добавить аккуратное упоминание или ссылку."
        )
    return " ".join(parts)


def build_source_digest_payload(text: str | None) -> dict[str, str]:
    return {"type": "text", "text": (text or "").strip()}


def build_source_digest_prompt_key(channel_id: int, context: str) -> str:
    digest = hashlib.sha1(context.encode("utf-8")).hexdigest()[:16]
    return f"source_digest:{int(channel_id)}:{digest}"


def build_source_digest_history_input(item_count: int, context: str) -> str:
    return f"Дайджест источников: {int(item_count)} материалов, контекст {len(context or '')} символов"
