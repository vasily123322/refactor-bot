from __future__ import annotations

from contextlib import suppress
from sqlalchemy.ext.asyncio import AsyncSession


class TimestampHelpersMixin:
    """Набор удобных методов для моделей со временем.

    Не добавляет колонок. Ожидает атрибуты created_at/updated_at, если они есть на модели.
    """

    def get_created_at(self):
        return getattr(self, "created_at", None)

    def get_updated_at(self):
        return getattr(self, "updated_at", None)

    async def touch(self, session: AsyncSession | None = None) -> None:
        """Установить updated_at в текущее время, если такой атрибут есть.

        Если передан session — выполнит flush/commit безопасно (опционально).
        """
        if hasattr(self, "updated_at"):
            from sqlalchemy import func

            setattr(self, "updated_at", func.now())
            if session is not None:
                with suppress(Exception):
                    await session.flush()


class OwnerHelpersMixin:
    """Методы для моделей с owner_id.

    Не добавляет колонок. Работает, если у модели есть поле owner_id.
    """

    def get_owner_id(self) -> int | None:
        try:
            val = getattr(self, "owner_id")
            return int(val) if val is not None else None
        except Exception:
            return None

    def set_owner_id(self, owner_id: int) -> None:
        if hasattr(self, "owner_id"):
            setattr(self, "owner_id", int(owner_id))

    def owned_by(self, owner_id: int) -> bool:
        try:
            cur = self.get_owner_id()
            return (cur is not None) and (int(cur) == int(owner_id))
        except Exception:
            return False


class ActivatableHelpersMixin:
    """Методы для моделей с is_active."""

    def is_active_bool(self) -> bool:
        return bool(getattr(self, "is_active", False))

    def activate(self) -> None:
        if hasattr(self, "is_active"):
            setattr(self, "is_active", True)

    def deactivate(self) -> None:
        if hasattr(self, "is_active"):
            setattr(self, "is_active", False)


class UuidHelpersMixin:
    """Методы для моделей с полем uuid (если оно есть)."""

    def get_uuid(self) -> str | None:
        val = getattr(self, "uuid", None)
        return str(val) if val is not None else None

    def ensure_uuid(self) -> None:
        if getattr(self, "uuid", None):
            return
        if hasattr(self, "uuid"):
            import uuid as _uuid

            setattr(self, "uuid", str(_uuid.uuid4()))
