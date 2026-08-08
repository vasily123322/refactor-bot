from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import Header, HTTPException, status

from app.api.studio.config import studio_config
from app.core.config import settings


@dataclass(frozen=True, slots=True)
class StudioPrincipal:
    tg_user_id: int
    username: str | None
    full_name: str | None
    auth_date: datetime


def _as_utc_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError("invalid Mini App auth_date") from exc


def validate_studio_init_data(
    raw_init_data: str,
    *,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> StudioPrincipal:
    """Validate Telegram Mini App initData and enforce a bounded replay window."""
    if not raw_init_data or not raw_init_data.strip():
        raise ValueError("Mini App init data is empty")

    parsed = safe_parse_webapp_init_data(settings.bot_token, raw_init_data)
    user = parsed.user
    if user is None:
        raise ValueError("Mini App init data has no user")

    auth_date = _as_utc_datetime(parsed.auth_date)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    max_age = int(max_age_seconds or studio_config.init_data_max_age_seconds)
    age = (current - auth_date).total_seconds()
    if age < -60:
        raise ValueError("Mini App init data is from the future")
    if age > max_age:
        raise ValueError("Mini App init data has expired")

    full_name = " ".join(
        value.strip()
        for value in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if isinstance(value, str) and value.strip()
    ) or None
    return StudioPrincipal(
        tg_user_id=int(user.id),
        username=getattr(user, "username", None),
        full_name=full_name,
        auth_date=auth_date,
    )


async def require_studio_principal(
    init_data: Annotated[
        str | None,
        Header(alias="X-Telegram-Init-Data"),
    ] = None,
) -> StudioPrincipal:
    if init_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram Mini App init data is required",
        )
    try:
        return validate_studio_init_data(init_data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Telegram Mini App init data",
        ) from exc
