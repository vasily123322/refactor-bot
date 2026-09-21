from __future__ import annotations

import sys
from functools import wraps

from loguru import logger

from app.core.redaction import redact_log_record


def with_context_logging(handler):
    """Декоратор для логирования контекста: user_id, chat_id, callback/data."""

    @wraps(handler)
    async def _wrap(*args, **kwargs):
        user_id = None
        chat_id = None
        data = None
        for a in args:
            try:
                u = getattr(a, "from_user", None)
                if u and user_id is None:
                    user_id = int(getattr(u, "id", 0) or 0)
                m = getattr(a, "message", None) or getattr(a, "chat", None)
                if m and chat_id is None:
                    chat_id = int(
                        getattr(m, "chat", getattr(a, "chat", None)).id
                        if hasattr(m, "chat")
                        else getattr(m, "id", 0)
                    )
                if hasattr(a, "data") and data is None:
                    data = getattr(a, "data")
            except Exception:
                continue
        logger.bind(user_id=user_id, chat_id=chat_id).info(
            f"handler={handler.__name__} data={data}"
        )
        return await handler(*args, **kwargs)

    return _wrap


def setup_logging(
    level: str = "INFO",
    *,
    file_enabled: bool = True,
    file_retention: str = "14 days",
) -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {extra} {message}",
        level=level,
        colorize=True,
        filter=redact_log_record,
        backtrace=False,
        diagnose=False,
    )
    if not file_enabled:
        return
    logger.add(
        "logs/bot.log",
        rotation="1 day",
        retention=file_retention,
        compression="zip",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra} {message}",
        filter=redact_log_record,
        backtrace=False,
        diagnose=False,
    )
