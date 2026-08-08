from __future__ import annotations

from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from loguru import logger

from app.core.config import settings


def build_fsm_storage(redis_dsn: str | None = None) -> BaseStorage:
    """Build persistent FSM storage when Redis is configured.

    Bot ID is part of every key so the main bot and managed/external bots can
    safely share the same Redis database without state collisions.
    """
    dsn = redis_dsn if redis_dsn is not None else settings.effective_redis_dsn()
    if not dsn:
        logger.warning(
            "FSM: REDIS_DSN is not configured; using non-persistent MemoryStorage"
        )
        return MemoryStorage()

    return RedisStorage.from_url(
        dsn,
        key_builder=DefaultKeyBuilder(with_bot_id=True),
    )
