from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine


class DatabaseSchemaOutOfDate(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AlembicSchemaState:
    managed: bool
    current_heads: tuple[str, ...]
    expected_heads: tuple[str, ...]

    @property
    def at_head(self) -> bool:
        return self.managed and set(self.current_heads) == set(self.expected_heads)


def _alembic_config() -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    return Config(str(repo_root / "alembic.ini"))


def _inspect_schema_sync(connection: Connection) -> AlembicSchemaState:
    expected_heads = tuple(
        sorted(ScriptDirectory.from_config(_alembic_config()).get_heads())
    )
    table_names = set(inspect(connection).get_table_names())
    if "alembic_version" not in table_names:
        return AlembicSchemaState(
            managed=False,
            current_heads=(),
            expected_heads=expected_heads,
        )

    current_heads = tuple(
        sorted(MigrationContext.configure(connection).get_current_heads())
    )
    return AlembicSchemaState(
        managed=True,
        current_heads=current_heads,
        expected_heads=expected_heads,
    )


async def inspect_alembic_schema(engine: AsyncEngine) -> AlembicSchemaState:
    async with engine.connect() as connection:
        return await connection.run_sync(_inspect_schema_sync)


async def bootstrap_database_schema(
    engine: AsyncEngine,
    *,
    unmanaged_initializer: Callable[[], Awaitable[None]],
) -> AlembicSchemaState:
    """Use Alembic as source of truth once a database has adopted it.

    Databases without an alembic_version table keep the historical bootstrap path.
    Once managed, runtime schema mutation is disabled and startup fails closed when
    the database is behind the revision scripts shipped with the application.
    """
    state = await inspect_alembic_schema(engine)
    if not state.managed:
        await unmanaged_initializer()
        return state

    if not state.at_head:
        current = ",".join(state.current_heads) or "base"
        expected = ",".join(state.expected_heads) or "<none>"
        raise DatabaseSchemaOutOfDate(
            "database Alembic revision is not at application head "
            f"(current={current}, expected={expected}); run `alembic upgrade head`"
        )
    return state
