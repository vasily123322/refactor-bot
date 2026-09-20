from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine


class DatabaseSchemaOutOfDate(RuntimeError):
    pass


class DatabaseSchemaShapeError(RuntimeError):
    pass


class DatabaseForeignKeyIntegrityError(RuntimeError):
    pass


class DatabaseSchemaUnmanaged(RuntimeError):
    pass


class DatabaseSchemaMigrationError(RuntimeError):
    pass


class DatabaseLegacyAdoptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AlembicSchemaState:
    managed: bool
    current_heads: tuple[str, ...]
    expected_heads: tuple[str, ...]
    table_names: tuple[str, ...]

    @property
    def at_head(self) -> bool:
        return self.managed and set(self.current_heads) == set(self.expected_heads)

    @property
    def empty(self) -> bool:
        return not self.table_names


def _alembic_config() -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    return Config(str(repo_root / "alembic.ini"))


def _inspect_schema_sync(connection: Connection) -> AlembicSchemaState:
    expected_heads = tuple(
        sorted(ScriptDirectory.from_config(_alembic_config()).get_heads())
    )
    table_names = tuple(sorted(inspect(connection).get_table_names()))
    if "alembic_version" not in table_names:
        return AlembicSchemaState(
            managed=False,
            current_heads=(),
            expected_heads=expected_heads,
            table_names=table_names,
        )

    current_heads = tuple(
        sorted(MigrationContext.configure(connection).get_current_heads())
    )
    return AlembicSchemaState(
        managed=True,
        current_heads=current_heads,
        expected_heads=expected_heads,
        table_names=table_names,
    )


def _upgrade_to_head_sync(connection: Connection) -> None:
    config = _alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def _stamp_head_sync(connection: Connection) -> None:
    config = _alembic_config()
    config.attributes["connection"] = connection
    command.stamp(config, "head")


def _diff_kind(diff: Any) -> str:
    if isinstance(diff, tuple) and diff:
        return str(diff[0])
    if isinstance(diff, list) and diff:
        return _diff_kind(diff[0])
    return type(diff).__name__


def _verify_exact_current_orm_schema_sync(connection: Connection) -> None:
    # Safe stamping is deliberately stricter than normal startup shape checks.
    # A legacy create_all() database may be adopted only when Alembic sees no
    # metadata drift at all: tables, columns, indexes and constraints must already
    # match the current ORM schema.
    import app.domain  # noqa: F401
    from app.core.db import Base

    migration_context = MigrationContext.configure(
        connection,
        opts={"compare_type": True},
    )
    differences = compare_metadata(migration_context, Base.metadata)
    if not differences:
        return

    kinds = ",".join(sorted({_diff_kind(diff) for diff in differences}))
    raise DatabaseLegacyAdoptionError(
        "unmanaged database cannot be safely adopted because its schema does not "
        "exactly match current ORM metadata "
        f"(differences={kinds or 'unknown'}); restore or repair the database from "
        "a verified backup and retry the adoption command; do not run "
        "`alembic stamp head` directly"
    )


def _verify_required_schema_shape_sync(connection: Connection) -> None:
    # Import the registry here so direct callers of app.core.schema do not depend on
    # dispatcher import order when validating current ORM requirements.
    import app.domain  # noqa: F401
    from app.core.db import Base

    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)

    missing_tables = sorted(expected_tables - actual_tables)
    missing_columns: list[str] = []
    for table_name in sorted(expected_tables & actual_tables):
        expected = set(Base.metadata.tables[table_name].columns.keys())
        actual = {str(column["name"]) for column in inspector.get_columns(table_name)}
        for column_name in sorted(expected - actual):
            missing_columns.append(f"{table_name}.{column_name}")

    if not missing_tables and not missing_columns:
        return

    parts: list[str] = []
    if missing_tables:
        parts.append("missing tables=" + ",".join(missing_tables[:20]))
    if missing_columns:
        parts.append("missing columns=" + ",".join(missing_columns[:20]))
    if len(missing_tables) > 20 or len(missing_columns) > 20:
        parts.append("additional mismatches omitted")
    raise DatabaseSchemaShapeError(
        "managed database schema does not satisfy current ORM requirements ("
        + "; ".join(parts)
        + "); apply the correct migration or repair the schema before startup"
    )


def _enable_sqlite_foreign_keys_on_connect(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


async def _verify_managed_schema_shape(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.run_sync(_verify_required_schema_shape_sync)


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


def _raise_if_not_at_head(state: AlembicSchemaState) -> None:
    if state.at_head:
        return
    current = ",".join(state.current_heads) or "base"
    expected = ",".join(state.expected_heads) or "<none>"
    raise DatabaseSchemaOutOfDate(
        "database Alembic revision is not at application head "
        f"(current={current}, expected={expected}); run `alembic upgrade head` "
        "before application startup"
    )


async def _upgrade_fresh_database_to_head(engine: AsyncEngine) -> None:
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade_to_head_sync)
    except Exception as exc:
        raise DatabaseSchemaMigrationError(
            "Alembic failed while initializing the fresh database; no legacy "
            "create_all/ad-hoc fallback was attempted. Fix the migration error and "
            "retry startup."
        ) from exc


async def _verify_managed_startup_state(
    engine: AsyncEngine,
    state: AlembicSchemaState,
) -> AlembicSchemaState:
    _raise_if_not_at_head(state)
    await _verify_managed_schema_shape(engine)
    await _verify_and_enable_sqlite_foreign_keys(engine)
    return state


async def bootstrap_database_schema(engine: AsyncEngine) -> AlembicSchemaState:
    """Establish the application startup schema contract.

    A truly empty database is initialized by Alembic to the shipped head. Existing
    Alembic-managed databases must already be at that head. A non-empty database
    without alembic_version is never mutated by runtime startup: legacy create_all()
    compatibility requires the explicit, validated adoption command.
    """

    state = await inspect_alembic_schema(engine)
    if state.managed:
        return await _verify_managed_startup_state(engine, state)

    if not state.empty:
        tables = ",".join(state.table_names[:10])
        if len(state.table_names) > 10:
            tables += ",..."
        raise DatabaseSchemaUnmanaged(
            "database contains schema objects but has no alembic_version "
            f"(tables={tables}); application startup refuses the legacy "
            "Base.metadata.create_all()/ad-hoc bootstrap path. Stop the application, "
            "take a verified backup, then run "
            "`python scripts/adopt_legacy_database.py` for a database created by "
            "the previous current-ORM create_all() bootstrap. If adoption rejects "
            "the schema, repair or migrate it explicitly; do not stamp it blindly."
        )

    await _upgrade_fresh_database_to_head(engine)
    migrated = await inspect_alembic_schema(engine)
    if not migrated.managed:
        raise DatabaseSchemaMigrationError(
            "fresh database migration completed without creating alembic_version; "
            "startup is refusing to continue"
        )
    return await _verify_managed_startup_state(engine, migrated)


async def adopt_legacy_database_schema(engine: AsyncEngine) -> AlembicSchemaState:
    """Safely adopt a legacy current-ORM create_all() database into Alembic.

    This is an explicit operator action, never a production startup fallback. The
    database must be unmanaged, non-empty, and exactly equivalent to current ORM
    metadata before Alembic head is stamped.
    """

    state = await inspect_alembic_schema(engine)
    if state.managed:
        return await _verify_managed_startup_state(engine, state)
    if state.empty:
        raise DatabaseLegacyAdoptionError(
            "legacy adoption is not for an empty database; use normal application "
            "startup or `alembic upgrade head` so Alembic creates the schema"
        )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(_verify_exact_current_orm_schema_sync)
            await connection.run_sync(_stamp_head_sync)
    except DatabaseLegacyAdoptionError:
        raise
    except Exception as exc:
        raise DatabaseLegacyAdoptionError(
            "Alembic could not stamp the validated legacy database at head; "
            "restore/repair from the verified backup and retry"
        ) from exc

    adopted = await inspect_alembic_schema(engine)
    if not adopted.managed or not adopted.at_head:
        raise DatabaseLegacyAdoptionError(
            "legacy adoption did not leave the database at the application Alembic "
            "head; restore/repair from the verified backup before startup"
        )
    return await _verify_managed_startup_state(engine, adopted)
