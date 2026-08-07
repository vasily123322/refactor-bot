from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PUBLICATION_PROFILES: dict[str, dict[str, str]] = {
    "default": {
        "title": "Обычный пост",
        "description": "универсальная структура без жёсткого сценария",
        "prompt": "Пиши как универсальный Telegram-пост: понятный хук, короткая основная часть, аккуратный вывод или CTA.",
    },
    "news": {
        "title": "Новости",
        "description": "коротко: что случилось, почему важно, что дальше",
        "prompt": "Профиль публикации: новость. Начни с факта/события, затем объясни почему это важно для аудитории и чем это может закончиться. Не драматизируй и не добавляй непроверенные детали.",
    },
    "sales": {
        "title": "Продажи",
        "description": "польза, возражения, мягкий призыв",
        "prompt": "Профиль публикации: продающий пост. Покажи проблему аудитории, конкретную пользу предложения, 1–2 сильных аргумента и мягкий CTA. Не обещай гарантированный результат.",
    },
    "analysis": {
        "title": "Разбор",
        "description": "контекст, причины, выводы",
        "prompt": "Профиль публикации: разбор. Дай контекст, разложи тему на 3–5 понятных пунктов, добавь выводы и практическое значение для читателя. Сохраняй экспертность без канцелярита.",
    },
    "meme": {
        "title": "Мемы / лёгкий пост",
        "description": "коротко, живо, с иронией без токсичности",
        "prompt": "Профиль публикации: лёгкий/мемный пост. Пиши коротко, живо и иронично, но без токсичности, унижения людей и спорных утверждений. Смысл должен быть понятен без длинного контекста.",
    },
}


def get_publication_profile(filters: Mapping[str, Any] | None) -> str:
    if not isinstance(filters, Mapping):
        return "default"
    profile = str(filters.get("publication_profile") or "default").strip()
    return profile if profile in PUBLICATION_PROFILES else "default"


def apply_publication_profile(filters: Mapping[str, Any] | None, profile: str) -> dict[str, Any]:
    if profile not in PUBLICATION_PROFILES:
        raise ValueError(f"Unknown publication profile: {profile}")
    next_filters = dict(filters or {})
    if profile == "default":
        next_filters.pop("publication_profile", None)
    else:
        next_filters["publication_profile"] = profile
    return next_filters


def build_publication_profile_text(filters: Mapping[str, Any] | None) -> str:
    profile = get_publication_profile(filters)
    if profile == "default":
        return ""
    cfg = PUBLICATION_PROFILES[profile]
    return f"Профиль публикации — {cfg['title']}: {cfg['prompt']}"


def get_publication_profile_menu_items() -> list[tuple[str, str]]:
    """Return ordered (code, cfg) tuples for the publication-profile menu."""
    order = ["default", "news", "sales", "analysis", "meme"]
    return [(code, PUBLICATION_PROFILES[code]) for code in order if code in PUBLICATION_PROFILES]


def build_publication_profile_menu_text(*, current_profile: str | None = None) -> str:
    cfg = PUBLICATION_PROFILES.get(current_profile or "default", PUBLICATION_PROFILES["default"])
    current_title = cfg["title"]

    lines = [
        "🧩 Профиль публикации",
        "",
        f"Текущий: {current_title}",
        f"Сценарий: {cfg['description']}",
        "",
        "Профиль задаёт структуру поста: новость, продажи, разбор или лёгкий формат. "
        "Он работает вместе с навыком ИИ и памятью канала.",
    ]
    return "\n".join(lines)


def build_publication_profile_button_rows(
    *,
    channel_id: int,
    current_profile: str | None = None,
) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for code, cfg in get_publication_profile_menu_items():
        prefix = "✅" if current_profile == code else "☑️"
        row.append((f"{prefix} {cfg['title']}", f"ai_set_publication_profile_{channel_id}_{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("← Назад", f"neu_text_{channel_id}")])
    return rows


def build_publication_profile_set_message(profile_code: str | None) -> str:
    if profile_code and profile_code in PUBLICATION_PROFILES:
        return f"✅ Профиль: {PUBLICATION_PROFILES[profile_code]['title']}"
    return "✅ Профиль обновлён"
