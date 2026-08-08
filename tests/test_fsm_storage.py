from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.core.fsm_storage import build_fsm_storage


def test_fsm_storage_falls_back_to_memory_without_redis() -> None:
    storage = build_fsm_storage("")
    assert isinstance(storage, MemoryStorage)


def test_fsm_storage_uses_redis_with_bot_scoped_keys() -> None:
    storage = build_fsm_storage("redis://localhost:6379/7")
    assert isinstance(storage, RedisStorage)
    assert isinstance(storage.key_builder, DefaultKeyBuilder)
    assert storage.key_builder.with_bot_id is True
    assert storage.redis.connection_pool.connection_kwargs["db"] == 7
