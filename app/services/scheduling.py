from __future__ import annotations
from datetime import datetime, timezone, timedelta


def as_utc(dt: datetime | None) -> datetime:
    """Вернуть aware-UTC для naive/aware входа; None -> now(UTC)."""
    if dt is None:
        return datetime.now(timezone.utc)
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_next_repeat_time(
    start_utc: datetime, repeat_seconds: int, after_utc: datetime
) -> datetime:
    """Первый момент > after_utc, шагая от start_utc по repeat_seconds."""
    next_when = start_utc
    step = max(1, int(repeat_seconds))
    while next_when <= after_utc:
        next_when = next_when + timedelta(seconds=step)
    return next_when


def cleanup_runtime_fields(payload: dict) -> dict:
    """Удалить временные поля из payload перед созданием повтора."""
    pl = dict(payload or {})
    pl.pop("result_ids", None)
    pl.pop("result_link", None)
    pl.pop("autodeleted", None)
    pl.pop("autodeleted_at", None)
    pl.pop("autodelete_at", None)
    return pl


def inherit_flags_for_repeat(orig: dict, base_post_id: int) -> dict:
    """Пробросить устойчивые флаги в новую запись повтора."""
    pl = dict(orig)
    if pl.get("autosign_applied"):
        pl["autosign_applied"] = True
    if pl.get("repeat_group_id") is None:
        pl["repeat_group_id"] = int(base_post_id)
    return pl
