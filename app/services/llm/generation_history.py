"""Small helpers for remembering AI generations inside FSM state."""

from __future__ import annotations

from typing import Any


MAX_GENERATION_TEXT_CHARS = 2000
DEFAULT_GENERATION_HISTORY_LIMIT = 5


def remember_generation(
    history: list[Any] | None,
    *,
    mode: str,
    input_text: str = "",
    generated_text: str,
    limit: int = DEFAULT_GENERATION_HISTORY_LIMIT,
) -> list[dict[str, str]]:
    """Return updated generation history with the newest successful result first."""

    clean_text = (generated_text or "").strip()
    existing = [dict(item) for item in (history or []) if isinstance(item, dict)]
    if not clean_text:
        return existing[: max(0, int(limit))]

    entry = {
        "mode": str(mode or "unknown")[:64],
        "input": str(input_text or "")[:1000],
        "text": clean_text[:MAX_GENERATION_TEXT_CHARS],
    }
    return [entry, *existing][: max(1, int(limit))]


def build_similar_generation_instruction(
    previous_text: str,
    *,
    user_hint: str = "",
) -> str:
    """Build a rewrite instruction that creates a similar post without copying facts."""

    prev = (previous_text or "").strip()[:MAX_GENERATION_TEXT_CHARS]
    hint = (user_hint or "").strip()
    hint_part = f"\nНовая вводная от пользователя: {hint}" if hint else ""
    return (
        "Создай новый похожий пост: сохрани структуру, темп, тон, длину и тип подачи, "
        "но не копируй факты, формулировки и конкретные детали исходного текста."
        f"{hint_part}\n\nИсходный удачный вариант:\n{prev}"
    )
