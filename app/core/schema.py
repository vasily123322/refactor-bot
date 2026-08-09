from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine


class DatabaseSchemaOutOfDate(RuntimeError):
    pass


class DatabaseForeignKeyIntegrityError(RuntimeError):
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


def _enable_sqlite_foreign_keys_on_connect(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


async def _verify_and_enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    if engine.dialect.name != "sqlite":
        return

    # `foreign_key_check` works even while enforcement is disabled. Audit historical
    # managed data before changing connection semantics; otherwise enabling PRAGMA
    # would leave pre-existing orphan rows silently grandfathered into the process.
    async with engine.connect() as connection:
        violations = await connection.exec_driver_sql("PRAGMA foreign_key_check")
        if violations.first() is not None:
            raise DatabaseForeignKeyIntegrityError(
                "managed SQLite database contains foreign key violations; "
                "repair the data before application startup"
            )

    sync_engine = engine.sync_engine
    if not event.contains(
        sync_engine,
        "connect",
        _enable_sqlite_foreign_keys_on_connect,
    ):
        event.listen(
            sync_engine,
            "connect",
            _enable_sqlite_foreign_keys_on_connect,
        )

    # Inspection may already have populated StaticPool. Dispose every pre-enforcement
    # connection so all subsequent application sessions are created through the PRAGMA
    # listener instead of reusing a connection with foreign_keys=OFF.
    await engine.dispose()

    async with engine.connect() as connection:
        enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
        if int(enabled.scalar_one() or 0) != 1:
            raise DatabaseForeignKeyIntegrityError(
                "managed SQLite foreign key enforcement could not be enabled"
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
    Managed SQLite additionally audits existing FK integrity before enforcing foreign
    keys on every subsequent connection.
    """
    state = await inspect_alembic_schema(engine)
    if not state.managed:
        # Inspection opens the database before the historical initializer runs.
        # Clear pooled connections (especially SQLite StaticPool) because legacy
        # bootstrap may atomically rename an old database file before create_all().
        await engine.dispose()
        await unmanaged_initializer()
        return state

    if not state.at_head:
        current = ",".join(state.current_heads) or "base"
        expected = ",".join(state.expected_heads) or "<none>"
        raise DatabaseSchemaOutOfDate(
            "database Alembic revision is not at application head "
            f"(current={current}, expected={expected}); run `alembic upgrade head`"
        )

    await _verify_and_enable_sqlite_foreign_keys(engine)
    return state
