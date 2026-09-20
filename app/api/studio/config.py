from __future__ import annotations

import os
from dataclasses import dataclass


_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False
    allowed = ", ".join(sorted(_TRUE_ENV_VALUES | _FALSE_ENV_VALUES))
    raise ValueError(f"{name} must be one of: {allowed}; got {raw!r}")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip()
    try:
        value = int(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc
    if value < minimum or value > maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}; got {value}"
        )
    return value


def _env_origins() -> tuple[str, ...]:
    raw = os.getenv("STUDIO_CORS_ORIGINS", "")
    return tuple(value.strip() for value in raw.split(",") if value.strip())


@dataclass(frozen=True, slots=True)
class StudioConfig:
    enabled: bool
    host: str
    port: int
    public_url: str | None
    init_data_max_age_seconds: int
    cors_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "StudioConfig":
        public_url = (os.getenv("STUDIO_PUBLIC_URL") or "").strip() or None
        return cls(
            enabled=_env_bool("STUDIO_ENABLED", False),
            host=(os.getenv("STUDIO_HOST") or "127.0.0.1").strip(),
            port=_env_int("STUDIO_PORT", 8080, minimum=1, maximum=65535),
            public_url=public_url,
            init_data_max_age_seconds=_env_int(
                "STUDIO_INIT_DATA_MAX_AGE_SECONDS",
                86400,
                minimum=60,
                maximum=7 * 86400,
            ),
            cors_origins=_env_origins(),
        )


studio_config = StudioConfig.from_env()
