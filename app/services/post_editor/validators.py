from __future__ import annotations

from typing import Tuple


def validate_payload(payload: dict) -> Tuple[bool, str | None]:
    """Базовые проверки контента поста.

    - Проверка длины текста/подписи
    - Ограничения Telegram: text<=4096, caption<=1024
    """
    t = (payload or {}).get("type")
    if t == "text":
        text = (payload.get("text") or "").strip()
        if not text:
            return False, "Текст пуст"
        if len(text) > 4096:
            return False, "Текст слишком длинный (лимит 4096)"
    elif t in {"photo", "video", "animation", "audio", "voice", "video_note", "album"}:
        cap = (payload.get("caption") or "").strip()
        if len(cap) > 1024:
            return False, "Подпись слишком длинная (лимит 1024)"
    else:
        return False, "Неподдерживаемый тип контента"
    return True, None
