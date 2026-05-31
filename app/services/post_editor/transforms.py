from __future__ import annotations


def normalize_payload(payload: dict) -> dict:
    """Нормализовать поля payload: обрезки, перенос caption->text при пустом типе и т.п."""
    if not payload:
        return {}
    res = dict(payload)
    try:
        if res.get("type") == "text":
            res["text"] = (res.get("text") or "").strip()
        elif res.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "video_note",
            "album",
        }:
            res["caption"] = (res.get("caption") or "").strip()
    except Exception:
        pass
    return res
