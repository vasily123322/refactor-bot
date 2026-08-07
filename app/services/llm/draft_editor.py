"""AI Draft Editor — prompts and helpers for post text transformations.

Maps editor actions (shorten, cta, channel_style, etc.) to structured
instructions passed to :meth:`AIGenerationService.improve_text`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DraftEditAction:
    """Single editable transformation the AI editor can apply."""

    callback: str  # callback_data suffix (e.g. "ai_improve_shorten")
    label: str  # human-readable button label with emoji
    instruction: str  # fed to improve_text(instruction=…)
    extra_system_note: str = ""  # appended to the system prompt


# Ordered list — also defines menu button order.
DRAFT_EDIT_ACTIONS: tuple[DraftEditAction, ...] = (
    DraftEditAction(
        callback="ai_improve_shorten",
        label="✂️ Сделай короче",
        instruction="Скороти текст примерно вдвое. Удали повторы и воду. Сохрани ключевые мысли и важные детали.",
    ),
    DraftEditAction(
        callback="ai_improve_cta",
        label="📢 Добавь продающий CTA",
        instruction=(
            "Добавь в конец текста сильный призыв к действию (CTA). "
            "CTA должен быть конкретным: подписаться, перейти, купить, проголосовать, написать в комментарии. "
            "Стиль CTA — дружелюбный, но настойчивый. Не используй кликбейт."
        ),
    ),
    DraftEditAction(
        callback="ai_improve_channel_style",
        label="🎨 В стиле канала",
        instruction=(
            "Перепиши текст так, чтобы он звучал в стиле этого канала. "
            "Используй тон и приёмы из примеров постов канала (если есть). "
            "Сохрани смысл, но адаптируй подачу."
        ),
        extra_system_note="Если в промпте есть примеры постов канала — придерживайся их стиля.",
    ),
    DraftEditAction(
        callback="ai_improve_news",
        label="📰 Как новость",
        instruction=(
            "Перепиши текст в формате новостного поста. "
            "Структура: заголовок-лид → ключевые факты → детали. "
            "Тон: нейтральный, информативный. Без эмоций и оценок."
        ),
    ),
    DraftEditAction(
        callback="ai_improve_analysis",
        label="🔍 Как разбор",
        instruction=(
            "Перепиши текст как экспертный разбор. "
            "Структура: что произошло → почему это важно → что будет дальше → вывод. "
            "Используй маркеры списков, цифры или эмодзи для структуры. "
            "Тон: уверенный, аналитический."
        ),
    ),
    DraftEditAction(
        callback="ai_improve_meme",
        label="😂 Как мем / юмор",
        instruction=(
            "Перепиши текст в юмористическом стиле. "
            "Добавь шутку, иронию, мем-отсылки, если уместно. "
            "Формат: 1-3 коротких абзаца с эмодзи. "
            "Не ломай смысл — юмор должен усилить сообщение, а не заменить его."
        ),
    ),
    DraftEditAction(
        callback="ai_improve_emoji",
        label="😊 Добавить эмодзи",
        instruction=(
            "Добавь уместные эмодзи в текст. "
            "Не переусердствуй — 1-2 эмодзи на абзац. "
            "Эмодзи должны усиливать смысл, а не отвлекать."
        ),
    ),
    DraftEditAction(
        callback="ai_improve_style",
        label="✨ Улучшить общий стиль",
        instruction=(
            "Улучши качество текста: исправь ошибки, повысь читаемость, "
            "убери канцеляризмы и повторы. Сохрани авторский стиль и смысл."
        ),
    ),
    DraftEditAction(
        callback="ai_improve_lengthen",
        label="📖 Удлинить",
        instruction=(
            "Расширь текст примерно в 1.5–2 раза. "
            "Добавь детали, примеры, объяснения. "
            "Не добавляй воду — каждая новая фраза должна нести смысл."
        ),
    ),
)


# Build fast lookup maps
_CALLBACK_TO_ACTION: dict[str, DraftEditAction] = {
    a.callback: a for a in DRAFT_EDIT_ACTIONS
}


def get_action(callback: str) -> Optional[DraftEditAction]:
    """Return the :class:`DraftEditAction` for a given callback_data."""
    return _CALLBACK_TO_ACTION.get(callback)


def build_instruction_for(callback: str) -> str:
    """Return instruction string for *callback*, or a generic fallback."""
    action = _CALLBACK_TO_ACTION.get(callback)
    if action:
        return action.instruction
    return "Улучши текст, сохранив смысл и стиль."


def build_extra_system_note(callback: str) -> str:
    """Return extra system-prompt note for *callback*, or empty string."""
    action = _CALLBACK_TO_ACTION.get(callback)
    if action:
        return action.extra_system_note
    return ""
