from __future__ import annotations


INTERFACE_SETTINGS_OPTIONS: list[dict[str, object]] = [
    {
        "key": "folders",
        "title": "Папки",
        "description": "если каналов много, распределите их по папкам для удобного доступа.",
        "default": False,
    },
    {
        "key": "hide_bottom_menu",
        "title": "Скрывать нижнее меню",
        "description": "если предпочитаете использовать только команды, отключите кнопки меню.",
        "default": False,
    },
    {
        "key": "remember_channel",
        "title": "Запоминать канал",
        "description": "при создании нескольких постов подряд бот автоматически подставляет канал из предыдущего поста. Если опция отключена, каждый раз нужно указывать канал.",
        "default": True,
    },
    {
        "key": "confirm_publish",
        "title": "Подтверждать публикацию",
        "description": "бот запрашивает подтверждение перед отправкой поста, чтобы избежать случайной публикации.",
        "default": True,
    },
    {
        "key": "ad_posts",
        "title": "Рекламные посты",
        "description": "простой интерфейс для публикации рекламы.",
        "default": False,
    },
    {
        "key": "ai_compose",
        "title": "Написать с ИИ",
        "description": "посты по вашему запросу с помощью ИИ.",
        "default": False,
    },
]


INTERFACE_SETTINGS_DEFAULTS: dict[str, bool] = {
    str(item["key"]): bool(item.get("default", False))
    for item in INTERFACE_SETTINGS_OPTIONS
}


INTERFACE_SETTING_KEYS: set[str] = {
    str(item["key"]) for item in INTERFACE_SETTINGS_OPTIONS
}


def merge_ui_settings(raw: dict[str, object] | None) -> dict[str, bool]:
    merged = dict(INTERFACE_SETTINGS_DEFAULTS)
    if not raw:
        return merged
    for key, value in raw.items():
        if key in merged:
            merged[key] = bool(value)
    return merged
